"""Tests for the remote-access dashboard JS (the Settings > Advanced card in
``remote-access.js`` and the ``restart_started`` handler in ``websocket.js``).

The web UI is plain browser JS with no bundler or JS test harness, so these
tests drive ``node`` from pytest exactly like ``test_web_js_swap_ui.py``: each
case evals the component source inside a minimal DOM/window/api stub and asserts
on rendered HTML strings and the state-machine's observable side effects (Swal
options, api.put/post calls, overlay + health-poll calls). Skipped when node is
not on PATH.

The harness (``_run_js`` + the DOM stub) is imported from the swap-ui suite so
there is a single copy. Free functions declared at the top of a component file
leak into the harness scope via the sloppy-mode ``eval`` and are callable
directly; module-level ``const`` state (``_remoteAccessState``) does NOT leak,
so tests seed it through the real entry point (``renderRemoteAccessCard`` with a
stubbed ``api.get``) rather than poking it directly.
"""
import json
import shutil

import pytest

from tests.unit.test_web_js_swap_ui import WEB_JS, _run_js

REMOTE_ACCESS_JS = WEB_JS / "components" / "remote-access.js"
WEBSOCKET_JS = WEB_JS / "websocket.js"
HEADER_JS = WEB_JS / "components" / "header.js"

EM_DASH = "—"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not installed"
)


def _run(tmp_path, snippet):
    return _run_js(tmp_path, snippet, js_file=REMOTE_ACCESS_JS)


# ---------------------------------------------------------------------------
# Source-level guards
# ---------------------------------------------------------------------------


def test_node_syntax_check():
    import subprocess

    proc = subprocess.run(
        ["node", "--check", str(REMOTE_ACCESS_JS)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr


def test_no_em_dashes_in_user_facing_copy(tmp_path):
    """Every rendered card + confirm-dialog string must be em-dash free (LLM
    tell, banned in user-facing copy). Checks the actual emitted copy, not the
    source, so exempt code comments (which use the conventional header dash) do
    not trip it."""
    result = _run(tmp_path, r"""
global.window.location = { hostname: 'host', port: '8321' };
const texts = [];
const push = (o) => ['title', 'html', 'confirmButtonText', 'cancelButtonText', 'inputPlaceholder']
    .forEach(k => { if (o && o[k]) texts.push(String(o[k])); });

// Card in every mode (off, tailscale w/ fallback, all interfaces).
texts.push(renderRemoteAccessCardHTML({ enabled: false, scope: 'tailscale',
    effective: { addresses: ['127.0.0.1'], tailscale_ip: null, fallback_reason: null } }));
texts.push(renderRemoteAccessCardHTML({ enabled: true, scope: 'tailscale',
    effective: { addresses: ['127.0.0.1', '100.64.12.7'], tailscale_ip: '100.64.12.7',
                 fallback_reason: 'no Tailscale IP detected' } }));
texts.push(renderRemoteAccessCardHTML({ enabled: true, scope: 'all',
    effective: { addresses: ['0.0.0.0'], tailscale_ip: null, fallback_reason: null } }));
texts.push(_remoteAccessLoadingHTML());
texts.push(_remoteAccessErrorHTML('some failure'));

// Every confirm variant.
push(_remoteAccessConfirmOptions({ enabled: false, scope: 'tailscale' }, { enabled: true, scope: 'tailscale' }, false));
push(_remoteAccessConfirmOptions({ enabled: false, scope: 'tailscale' }, { enabled: true, scope: 'all' }, false));
push(_remoteAccessConfirmOptions({ enabled: true, scope: 'tailscale' }, { enabled: true, scope: 'all' }, false));
push(_remoteAccessConfirmOptions({ enabled: true, scope: 'all' }, { enabled: true, scope: 'tailscale' }, false));
push(_remoteAccessConfirmOptions({ enabled: true, scope: 'tailscale' }, { enabled: false, scope: 'tailscale' }, false));
push(_remoteAccessConfirmOptions({ enabled: true, scope: 'tailscale' }, { enabled: false, scope: 'tailscale' }, true));
// Scope-switch-to-tailscale-from-LAN-IP lockout variant (enabled stays true).
push(_remoteAccessConfirmOptions({ enabled: true, scope: 'all' }, { enabled: true, scope: 'tailscale' }, true));

out({ combined: texts.join('\n') });
""")
    combined = result["combined"]
    assert EM_DASH not in combined
    # Sanity: the copy we expect is actually present (guards a silent empty join).
    assert "Tailscale ACL" in combined
    assert "without authentication" in combined
    assert "will not reconnect" in combined
    # The scope-switch lockout copy cites the Tailscale address, not "turning off".
    assert "Tailscale address" in combined


# ---------------------------------------------------------------------------
# Shared lockout rule: window.remoteAccessLockout(pending, hostname)
# ---------------------------------------------------------------------------


class TestLockoutRule:
    """Directly exercise the deterministic rule that BOTH the confirm/overlay flow
    and the websocket.js restart_started handler consume, so the two can never
    disagree about whether applying a change strands THIS browser."""

    def test_shared_rule_matrix(self, tmp_path):
        result = _run(tmp_path, r"""
const L = (pending, host) => remoteAccessLockout(pending, host);
// It is exported on window for websocket.js to consume.
const exported = (typeof window !== 'undefined') && (window.remoteAccessLockout === remoteAccessLockout);
out({
    exported,
    // Loopback origin is never a lockout, whatever the pending change.
    loopback_disable: L({ enabled: false, scope: 'tailscale' }, '127.0.0.1'),
    loopback_ts_localhost: L({ enabled: true, scope: 'tailscale' }, 'localhost'),
    loopback_v6: L({ enabled: false, scope: 'tailscale' }, '::1'),
    loopback_empty: L({ enabled: false, scope: 'tailscale' }, ''),
    // Turning remote access OFF strands every non-loopback origin.
    disable_from_lan: L({ enabled: false, scope: 'tailscale' }, '192.168.1.5'),
    disable_from_tailnet: L({ enabled: false, scope: 'all' }, '100.64.12.7'),
    disable_from_magicdns: L({ enabled: false, scope: 'tailscale' }, 'machine.tailnet.ts.net'),
    // scope 'all' binds every interface -> never a lockout, from anywhere.
    all_from_lan: L({ enabled: true, scope: 'all' }, '192.168.1.5'),
    all_from_tailnet: L({ enabled: true, scope: 'all' }, '100.64.12.7'),
    all_from_magicdns: L({ enabled: true, scope: 'all' }, 'box.ts.net'),
    // scope 'tailscale' from a bare LAN/other IPv4 literal outside CGNAT -> lockout.
    ts_from_lan: L({ enabled: true, scope: 'tailscale' }, '192.168.1.5'),
    ts_from_public_ip: L({ enabled: true, scope: 'tailscale' }, '8.8.8.8'),
    // ...but a tailnet IP (100.64.0.0/10) is kept -> not a lockout.
    ts_from_cgnat: L({ enabled: true, scope: 'tailscale' }, '100.64.12.7'),
    // MagicDNS names + single-label shortnames are not IPv4 literals -> covered.
    ts_from_magicdns: L({ enabled: true, scope: 'tailscale' }, 'machine.tailnet.ts.net'),
    ts_from_shortname: L({ enabled: true, scope: 'tailscale' }, 'mymachine'),
    // CGNAT boundaries: 100.63.x is below (lockout), 100.127.255.255 is the top
    // of the /10 (covered), 100.128.x is above (lockout), 100.64.0.0 is the base
    // (covered).
    ts_boundary_low: L({ enabled: true, scope: 'tailscale' }, '100.63.255.255'),
    ts_boundary_top: L({ enabled: true, scope: 'tailscale' }, '100.127.255.255'),
    ts_boundary_above: L({ enabled: true, scope: 'tailscale' }, '100.128.0.0'),
    ts_boundary_base: L({ enabled: true, scope: 'tailscale' }, '100.64.0.0'),
    // Malformed hostname strings must not throw; all resolve to "covered" (false).
    malformed: [
        '999.1.1.1', '1.2.3', '1.2.3.4.5', '100.64.12.7.8', 'a.b.c.d',
        '...', '100..1.1', '12 3.4.5.6', '::ffff:192.168.1.5', 'host:8321',
        null, undefined,
    ].map(h => L({ enabled: true, scope: 'tailscale' }, h)),
});
""")
        assert result["exported"] is True
        # Loopback: never a lockout.
        assert result["loopback_disable"] is False
        assert result["loopback_ts_localhost"] is False
        assert result["loopback_v6"] is False
        assert result["loopback_empty"] is False
        # Disable-from-remote: always a lockout.
        assert result["disable_from_lan"] is True
        assert result["disable_from_tailnet"] is True
        assert result["disable_from_magicdns"] is True
        # scope 'all': never a lockout.
        assert result["all_from_lan"] is False
        assert result["all_from_tailnet"] is False
        assert result["all_from_magicdns"] is False
        # scope 'tailscale' from a bare/other IP: lockout.
        assert result["ts_from_lan"] is True
        assert result["ts_from_public_ip"] is True
        # scope 'tailscale' from a tailnet IP / MagicDNS / shortname: not a lockout.
        assert result["ts_from_cgnat"] is False
        assert result["ts_from_magicdns"] is False
        assert result["ts_from_shortname"] is False
        # CGNAT boundary behavior.
        assert result["ts_boundary_low"] is True
        assert result["ts_boundary_top"] is False
        assert result["ts_boundary_above"] is True
        assert result["ts_boundary_base"] is False
        # Every malformed string is treated as covered and none threw.
        assert result["malformed"] == [False] * 12


# ---------------------------------------------------------------------------
# Rendering: loading -> populated, scope picker, status line, error
# ---------------------------------------------------------------------------


class TestRender:
    def test_loading_then_populated(self, tmp_path):
        result = _run(tmp_path, r"""
(async () => {
    let resolveGet;
    global.api.get = () => new Promise(r => { resolveGet = r; });
    const container = __makeEl('div');
    const p = renderRemoteAccessCard(container);
    const loadingHtml = container.innerHTML;   // synchronous, before await
    resolveGet({
        enabled: false, scope: 'tailscale',
        effective: { mode: 'loopback', addresses: ['127.0.0.1'],
                     tailscale_ip: null, fallback_reason: null },
    });
    await p;
    const populatedHtml = container.innerHTML;
    out({ loadingHtml, populatedHtml });
    process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
""")
        assert "Loading remote access settings" in result["loadingHtml"]
        pop = result["populatedHtml"]
        assert "Remote access" in pop
        assert "Allow remote access" in pop
        # Status line reads the effective bind, not the saved toggle.
        assert "Listening on" in pop
        assert "127.0.0.1" in pop
        # Scope picker is hidden while remote access is off.
        assert 'name="remote-access-scope"' not in pop

    def test_enabled_shows_scope_picker_and_url_hint(self, tmp_path):
        result = _run(tmp_path, r"""
global.window.location = { hostname: 'localhost', port: '8321' };
const html = renderRemoteAccessCardHTML({
    enabled: true, scope: 'tailscale',
    effective: { mode: 'tailscale', addresses: ['127.0.0.1', '100.64.12.7'],
                 tailscale_ip: '100.64.12.7', fallback_reason: null },
});
out({ html });
""")
        html = result["html"]
        assert 'name="remote-access-scope"' in html
        assert "Tailscale only" in html
        assert "Recommended" in html
        assert "All interfaces" in html
        # URL hint + Tailscale annotation come from the effective state. The
        # address is mono and the "(Tailscale)" annotation is a separate prose
        # span, so they are present but not one contiguous string.
        assert "http://100.64.12.7:8321" in html
        assert "100.64.12.7" in html
        assert "(Tailscale)" in html

    def test_fallback_reason_renders_warning_chip(self, tmp_path):
        result = _run(tmp_path, r"""
const reason = "Remote access is on, but no Tailscale IP was detected at startup; listening on loopback only.";
const html = renderRemoteAccessCardHTML({
    enabled: true, scope: 'tailscale',
    effective: { mode: 'tailscale', addresses: ['127.0.0.1'],
                 tailscale_ip: null, fallback_reason: reason },
});
out({ html });
""")
        html = result["html"]
        assert "no Tailscale IP was detected" in html
        # Yellow warning styling for the fallback chip.
        assert "text-yellow-300" in html

    def test_fetch_failure_renders_error_with_retry(self, tmp_path):
        result = _run(tmp_path, r"""
(async () => {
    global.api.get = async () => { throw new Error('boom-503'); };
    const container = __makeEl('div');
    await renderRemoteAccessCard(container);
    out({ html: container.innerHTML });
    process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
""")
        html = result["html"]
        assert "Could not load remote access settings" in html
        assert "boom-503" in html
        assert 'id="btn-remote-access-retry"' in html


# ---------------------------------------------------------------------------
# State machine: confirm -> PUT -> POST restart -> overlay
# ---------------------------------------------------------------------------


class TestSaveFlow:
    def test_confirm_puts_then_posts_then_overlay_polls(self, tmp_path):
        result = _run(tmp_path, r"""
(async () => {
    global.window.location = { hostname: 'localhost', port: '8321' };
    global.api.get = async () => ({
        enabled: false, scope: 'tailscale',
        effective: { mode: 'loopback', addresses: ['127.0.0.1'],
                     tailscale_ip: null, fallback_reason: null },
    });
    const container = __makeEl('div');
    await renderRemoteAccessCard(container);   // saved = disabled

    global.Swal = { fire: async () => ({ isConfirmed: true, value: 1 }) };
    const order = [];
    const puts = []; global.api.put = async (u, b) => { order.push('put'); puts.push([u, b]); return {}; };
    const posts = []; global.api.post = async (u) => { order.push('post'); posts.push(u); return {}; };
    const modals = []; global._showUpgradeModal = (m) => modals.push(m);
    let polled = 0; global._startHealthPolling = () => { polled++; };
    let terminal = 0; global._showRestartTerminal = () => { terminal++; };

    const res = await _applyRemoteAccessChange(container, { enabled: true, scope: 'tailscale' });
    out({ order, puts, posts, modals, polled, terminal,
          applied: res.applied, lockout: res.lockout });
    process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
""")
        assert result["order"] == ["put", "post"]
        assert result["puts"] == [
            ["/api/settings/remote-access", {"enabled": True, "scope": "tailscale"}]
        ]
        assert result["posts"] == ["/api/settings/remote-access/restart"]
        assert len(result["modals"]) == 1
        assert "Applying network settings" in result["modals"][0]
        assert result["polled"] == 1
        assert result["terminal"] == 0
        assert result["applied"] is True
        assert result["lockout"] is False

    def test_cancel_reverts_without_saving(self, tmp_path):
        result = _run(tmp_path, r"""
(async () => {
    global.window.location = { hostname: 'localhost', port: '8321' };
    global.api.get = async () => ({
        enabled: true, scope: 'tailscale',
        effective: { mode: 'tailscale', addresses: ['127.0.0.1', '100.64.12.7'],
                     tailscale_ip: '100.64.12.7', fallback_reason: null },
    });
    const container = __makeEl('div');
    await renderRemoteAccessCard(container);   // saved = enabled + tailscale

    global.Swal = { fire: async () => ({ isConfirmed: false }) };   // dismissed
    const puts = []; global.api.put = async (u, b) => { puts.push([u, b]); return {}; };

    const res = await _applyRemoteAccessChange(container, { enabled: true, scope: 'all' });
    out({ putCount: puts.length, reverted: res.reverted, applied: res.applied,
          revertedHtml: container.innerHTML });
    process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
""")
        assert result["putCount"] == 0
        assert result["reverted"] is True
        assert result["applied"] is False
        # Reverted card reflects the SAVED scope (tailscale active), not 'all'.
        assert "border-blue-500" in result["revertedHtml"]

    def test_all_interfaces_variant_requires_checkbox(self, tmp_path):
        result = _run(tmp_path, r"""
(async () => {
    global.window.location = { hostname: 'localhost', port: '8321' };
    global.api.get = async () => ({
        enabled: true, scope: 'tailscale',
        effective: { mode: 'tailscale', addresses: ['127.0.0.1', '100.64.12.7'],
                     tailscale_ip: '100.64.12.7', fallback_reason: null },
    });
    const container = __makeEl('div');
    await renderRemoteAccessCard(container);   // saved = enabled + tailscale

    let opts = null;
    global.Swal = { fire: async (o) => { opts = o; return { isConfirmed: false }; } };
    const puts = []; global.api.put = async (u, b) => { puts.push([u, b]); return {}; };

    await _applyRemoteAccessChange(container, { enabled: true, scope: 'all' });
    out({
        input: opts && opts.input,
        hasValidator: !!(opts && opts.inputValidator),
        confirmClass: opts && opts.customClass && opts.customClass.confirmButton,
        icon: opts && opts.icon,
        placeholder: (opts && opts.inputPlaceholder) || '',
        html: (opts && opts.html) || '',
        putCount: puts.length,
    });
    process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
""")
        assert result["input"] == "checkbox"
        assert result["hasValidator"] is True
        # Danger confirm uses the customClass (inline confirmButtonColor is
        # swallowed by swal-dark.css's !important base rule).
        assert result["confirmClass"] == "swal-confirm-danger"
        assert result["icon"] == "warning"
        # The mandatory acknowledgment is the checkbox label.
        assert "without authentication" in result["placeholder"]
        # The body still spells out the exposure.
        assert "no login" in result["html"]
        assert result["putCount"] == 0   # dismissed, nothing saved

    def test_put_failure_reverts_and_shows_error(self, tmp_path):
        result = _run(tmp_path, r"""
(async () => {
    global.window.location = { hostname: 'localhost', port: '8321' };
    global.api.get = async () => ({
        enabled: false, scope: 'tailscale',
        effective: { mode: 'loopback', addresses: ['127.0.0.1'],
                     tailscale_ip: null, fallback_reason: null },
    });
    const container = __makeEl('div');
    await renderRemoteAccessCard(container);

    global.Swal = { fire: async () => ({ isConfirmed: true }) };
    global.api.put = async () => { throw new Error('db locked'); };
    let posted = 0; global.api.post = async () => { posted++; return {}; };
    const errors = []; global._showUpgradeError = (m) => errors.push(m);
    let polled = 0; global._startHealthPolling = () => { polled++; };

    const res = await _applyRemoteAccessChange(container, { enabled: true, scope: 'tailscale' });
    out({ posted, errors, polled, reverted: res.reverted, applied: res.applied,
          isError: res.error });
    process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
""")
        assert result["posted"] == 0             # PUT failed, restart never fired
        assert len(result["errors"]) == 1
        assert "db locked" in result["errors"][0]
        assert result["polled"] == 0
        assert result["reverted"] is True
        assert result["applied"] is False
        assert result["isError"] is True


class TestLockout:
    def test_remote_disable_shows_terminal_and_does_not_poll(self, tmp_path):
        result = _run(tmp_path, r"""
(async () => {
    // Browsing via the Tailscale IP (non-loopback origin).
    global.window.location = { hostname: '100.64.12.7', port: '8321' };
    global.api.get = async () => ({
        enabled: true, scope: 'tailscale',
        effective: { mode: 'tailscale', addresses: ['127.0.0.1', '100.64.12.7'],
                     tailscale_ip: '100.64.12.7', fallback_reason: null },
    });
    const container = __makeEl('div');
    await renderRemoteAccessCard(container);   // saved = enabled

    let confirmOpts = null;
    global.Swal = { fire: async (o) => { confirmOpts = o; return { isConfirmed: true }; } };
    const puts = []; global.api.put = async (u, b) => { puts.push([u, b]); return {}; };
    const posts = []; global.api.post = async (u) => { posts.push(u); return {}; };
    const terminals = []; global._showRestartTerminal = (m) => terminals.push(m);
    const modals = []; global._showUpgradeModal = (m) => modals.push(m);
    let polled = 0; global._startHealthPolling = () => { polled++; };

    const res = await _applyRemoteAccessChange(container, { enabled: false, scope: 'tailscale' });
    out({
        confirmTitle: confirmOpts && confirmOpts.title,
        confirmHtml: (confirmOpts && confirmOpts.html) || '',
        putCount: puts.length, postCount: posts.length,
        terminals, modals, polled,
        lockout: res.lockout, applied: res.applied,
    });
    process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
""")
        assert result["lockout"] is True
        assert result["applied"] is True
        assert result["putCount"] == 1
        assert result["postCount"] == 1
        # Terminal message shown, no misleading spinner-poll.
        assert len(result["terminals"]) == 1
        assert "will not reconnect" in result["terminals"][0]
        assert result["polled"] == 0
        # No plain upgrade modal (that path would poll).
        assert len(result["modals"]) == 0
        # The confirm warned about disconnecting and cited the loopback URL.
        assert "127.0.0.1" in result["confirmHtml"]

    def test_local_disable_is_not_a_lockout(self, tmp_path):
        result = _run(tmp_path, r"""
(async () => {
    global.window.location = { hostname: '127.0.0.1', port: '8321' };
    global.api.get = async () => ({
        enabled: true, scope: 'tailscale',
        effective: { mode: 'tailscale', addresses: ['127.0.0.1', '100.64.12.7'],
                     tailscale_ip: '100.64.12.7', fallback_reason: null },
    });
    const container = __makeEl('div');
    await renderRemoteAccessCard(container);

    global.Swal = { fire: async () => ({ isConfirmed: true }) };
    global.api.put = async () => ({});
    global.api.post = async () => ({});
    const terminals = []; global._showRestartTerminal = (m) => terminals.push(m);
    const modals = []; global._showUpgradeModal = (m) => modals.push(m);
    let polled = 0; global._startHealthPolling = () => { polled++; };

    const res = await _applyRemoteAccessChange(container, { enabled: false, scope: 'tailscale' });
    out({ terminals: terminals.length, modals: modals.length, polled, lockout: res.lockout });
    process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
""")
        assert result["lockout"] is False
        assert result["terminals"] == 0
        assert result["modals"] == 1     # normal restart overlay
        assert result["polled"] == 1

    def test_restart_409_is_not_an_error_and_still_shows_overlay(self, tmp_path):
        """A 409 on POST restart (another tab already applying) is NOT a failure:
        the setting saved and a restart is in flight, so show the overlay, do not
        pop a scary error, and do not revert — mirroring the upgrade flow."""
        result = _run(tmp_path, r"""
(async () => {
    global.window.location = { hostname: '127.0.0.1', port: '8321' };
    global.api.get = async () => ({
        enabled: false, scope: 'tailscale',
        effective: { mode: 'loopback', addresses: ['127.0.0.1'],
                     tailscale_ip: null, fallback_reason: null },
    });
    const container = __makeEl('div');
    await renderRemoteAccessCard(container);

    global.Swal = { fire: async () => ({ isConfirmed: true }) };
    let putCount = 0; global.api.put = async () => { putCount++; return {}; };
    // POST rejects with a 409 (ApiError-shaped: carries .status).
    global.api.post = async () => { const e = new Error('Restart already in progress'); e.status = 409; throw e; };
    const modals = []; global._showUpgradeModal = (m) => modals.push(m);
    const errors = []; global._showUpgradeError = (m) => errors.push(m);
    let polled = 0; global._startHealthPolling = () => { polled++; };

    const res = await _applyRemoteAccessChange(container, { enabled: true, scope: 'tailscale' });
    out({
        applied: res.applied, reverted: res.reverted, error: res.error || false,
        putCount, modals: modals.length, errors: errors.length, polled,
    });
    process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
""")
        assert result["applied"] is True
        assert result["reverted"] is False
        assert result["error"] is False          # 409 is not an error
        assert result["putCount"] == 1
        assert result["errors"] == 0             # no scary error dialog
        assert result["modals"] == 1             # overlay still shown
        assert result["polled"] == 1

    def test_terminal_message_helper_is_shared_and_keyed_on_enabled(self, tmp_path):
        """remoteAccessTerminalMessage is the single source both the inline apply
        path and the websocket handler use, so their terminal copy can't drift."""
        result = _run(tmp_path, r"""
out({
    exported: typeof global.window.remoteAccessTerminalMessage === 'function',
    disabled: remoteAccessTerminalMessage({ enabled: false }),
    scopeSwitch: remoteAccessTerminalMessage({ enabled: true, scope: 'tailscale' }),
});
""")
        assert result["exported"] is True
        assert "will not reconnect" in result["disabled"]
        assert "off" in result["disabled"].lower()
        assert "will not reconnect" in result["scopeSwitch"]
        assert "Tailscale address" in result["scopeSwitch"]

    def test_scope_switch_from_lan_ip_shows_terminal_and_does_not_poll(self, tmp_path):
        """Narrowing scope to Tailscale-only while browsing from a bare LAN IP is a
        lockout even though remote access stays ON: the tailnet-only bind drops the
        LAN origin, so this page will not reconnect. Terminal message, no poll."""
        result = _run(tmp_path, r"""
(async () => {
    // Browsing via a bare LAN IP (not loopback, not tailnet). Server currently
    // binds all interfaces; narrowing to Tailscale-only strands this origin.
    global.window.location = { hostname: '192.168.1.5', port: '8321' };
    global.api.get = async () => ({
        enabled: true, scope: 'all',
        effective: { mode: 'all', addresses: ['0.0.0.0'],
                     tailscale_ip: '100.64.12.7', fallback_reason: null },
    });
    const container = __makeEl('div');
    await renderRemoteAccessCard(container);   // saved = enabled + all

    let confirmOpts = null;
    global.Swal = { fire: async (o) => { confirmOpts = o; return { isConfirmed: true }; } };
    const puts = []; global.api.put = async (u, b) => { puts.push([u, b]); return {}; };
    const posts = []; global.api.post = async (u) => { posts.push(u); return {}; };
    const terminals = []; global._showRestartTerminal = (m) => terminals.push(m);
    const modals = []; global._showUpgradeModal = (m) => modals.push(m);
    let polled = 0; global._startHealthPolling = () => { polled++; };

    const res = await _applyRemoteAccessChange(container, { enabled: true, scope: 'tailscale' });
    out({
        lockout: res.lockout, applied: res.applied,
        putCount: puts.length, postCount: posts.length,
        putBody: puts[0] ? puts[0][1] : null,
        confirmTitle: confirmOpts && confirmOpts.title,
        confirmHtml: (confirmOpts && confirmOpts.html) || '',
        confirmClass: confirmOpts && confirmOpts.customClass && confirmOpts.customClass.confirmButton,
        terminals, modals, polled,
    });
    process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
""")
        assert result["lockout"] is True
        assert result["applied"] is True
        # The change is still saved + restart still fired, exactly like disable.
        assert result["putCount"] == 1
        assert result["postCount"] == 1
        assert result["putBody"] == {"enabled": True, "scope": "tailscale"}
        # Terminal message shown, deliberately no spinner-poll (never reconnects).
        assert len(result["terminals"]) == 1
        assert "will not reconnect" in result["terminals"][0]
        assert "Tailscale address" in result["terminals"][0]
        assert result["polled"] == 0
        assert len(result["modals"]) == 0
        # Confirm copy is the scope-switch variant: cites loopback + the Tailscale
        # address, and does NOT falsely claim remote access is being turned off.
        assert result["confirmClass"] == "swal-confirm-danger"
        assert "127.0.0.1" in result["confirmHtml"]
        assert "Tailscale address" in result["confirmHtml"]
        assert "will not reconnect" in result["confirmHtml"]
        assert "Turning off remote access" not in result["confirmHtml"]


class TestTerminalModalContract:
    """The REAL header.js modal functions (not the global stubs the lockout
    tests use). This is the layer the lockout bug hid in: the terminal modal
    reuses the #upgrade-modal element, and the WS onclose handler starts health
    polling whenever that element is present. A terminal modal must mark itself
    so the disconnect poll and _startHealthPolling both leave it alone, or a
    remote page that turned remote access off gets its terminal message
    clobbered by 'Restarting...' and a doomed poll against a dead origin."""

    def test_terminal_marks_modal_and_blocks_poll_until_pollable_modal_clears_it(
        self, tmp_path
    ):
        snippet = r"""
// _startHealthPolling touches setInterval/fetch only if the terminal guard
// FAILS. Stub them so a regression fails by assertion, not a node crash.
let intervalsStarted = 0;
global.setInterval = () => { intervalsStarted++; return 1; };
global.clearInterval = () => {};
global.fetch = async () => ({ ok: false });

const msgText = () => { const m = global.document.getElementById('upgrade-modal-msg'); return m ? m.textContent : null; };
// `?? null`: a cleared marker is `undefined`, which JSON.stringify would drop
// (turning {terminal: undefined} into {}); null round-trips so the assert can
// distinguish "marker cleared" from "key never emitted".
const modalTerminal = () => { const md = global.document.getElementById('upgrade-modal'); return md ? (md.dataset.terminal ?? null) : null; };

// 1. Terminal modal marks itself and shows its message.
_showRestartTerminal('Remote access is off. This page will not reconnect.');
const afterTerminal = { terminal: modalTerminal(), msg: msgText() };

// 2. The WS disconnect guard: it polls only when the modal is NOT terminal.
//    Reproduce the exact websocket.js onclose condition against the real modal.
const md1 = global.document.getElementById('upgrade-modal');
const guardWouldPollWhileTerminal = !!(md1 && md1.dataset.terminal !== '1');

// 3. _startHealthPolling itself is a no-op on a terminal modal: the message
//    must NOT flip to 'Restarting...', and no interval starts.
_startHealthPolling();
const afterPollAttempt = { msg: msgText(), intervals: intervalsStarted };

// 4. A pollable modal (_showUpgradeModal) clears the terminal marker, so a
//    later real restart on the SAME element polls normally again.
_showUpgradeModal('Applying network settings...');
const afterPollable = { terminal: modalTerminal() };
const guardWouldPollAfterPollable = (() => {
    const md = global.document.getElementById('upgrade-modal');
    return !!(md && md.dataset.terminal !== '1');
})();
_startHealthPolling();
const afterSecondPoll = { msg: msgText(), intervals: intervalsStarted };

out({
    afterTerminal, guardWouldPollWhileTerminal,
    afterPollAttempt, afterPollable, guardWouldPollAfterPollable,
    afterSecondPoll,
});
"""
        result = _run_js(tmp_path, snippet, js_file=HEADER_JS)
        # 1. Terminal modal is marked and shows its own message.
        assert result["afterTerminal"]["terminal"] == "1"
        assert "will not reconnect" in result["afterTerminal"]["msg"]
        # 2. The disconnect guard would NOT poll while the modal is terminal.
        assert result["guardWouldPollWhileTerminal"] is False
        # 3. _startHealthPolling no-ops: message stays terminal, no interval armed.
        assert "will not reconnect" in result["afterPollAttempt"]["msg"]
        assert result["afterPollAttempt"]["intervals"] == 0
        # 4. A pollable modal clears the marker; polling resumes for a real restart.
        assert result["afterPollable"]["terminal"] is None
        assert result["guardWouldPollAfterPollable"] is True
        assert result["afterSecondPoll"]["msg"] == "Restarting…"
        assert result["afterSecondPoll"]["intervals"] == 1

    def test_start_health_polling_is_idempotent(self, tmp_path):
        """The initiating tab calls _startHealthPolling both inline and from its
        own WS restart_started broadcast. A second call while one is already
        running must be a no-op (one interval), not a second overlapping poller."""
        snippet = r"""
let intervalsStarted = 0;
global.setInterval = () => { intervalsStarted++; return intervalsStarted; };
global.clearInterval = () => {};
global.fetch = async () => ({ ok: false });

_startHealthPolling();   // first: arms one interval
_startHealthPolling();   // second: must be a no-op while the first runs
out({ intervals: intervalsStarted });
"""
        result = _run_js(tmp_path, snippet, js_file=HEADER_JS)
        assert result["intervals"] == 1


# ---------------------------------------------------------------------------
# websocket.js restart_started handler
# ---------------------------------------------------------------------------


class TestWebsocketRestartHandler:
    def test_restart_started_branches(self, tmp_path):
        # Install the SAME shared lockout rule the browser uses: remote-access.js
        # defines window.remoteAccessLockout and websocket.js consumes it. Evaling
        # the REAL source (not a re-implementation) is what proves this handler and
        # the settings confirm dialog can never disagree — one rule under test.
        # The eval is safe and intentional: it loads only this repo's first-party
        # remote-access.js (never external input), the same non-module-browser-JS
        # loading pattern the harness itself uses (see test_web_js_swap_ui.py).
        snippet = r"""
eval(require('fs').readFileSync(__RA_PATH__, 'utf8'));

const ws = global.__jackedWS;
const dispatch = (type, payload) =>
    (ws.handlers[type] || []).forEach(cb => cb({ type, payload }));

let terminals = [], modals = [], polled = 0;
global._showRestartTerminal = (m) => terminals.push(m);
global._showUpgradeModal = (m) => modals.push(m);
global._startHealthPolling = () => { polled++; };
const snap = () => ({ terminals: terminals.length, modals: modals.length, polled });
const reset = () => { terminals = []; modals = []; polled = 0; };

// 1. Remote (tailnet) origin + disabled -> terminal message, no polling.
global.location = { hostname: '100.64.12.7' };
dispatch('restart_started', { message: 'Applying network settings...', enabled: false, scope: 'tailscale' });
const lockout = snap();

// 2. Local origin + disabled -> normal overlay + poll (not a lockout).
reset();
global.location = { hostname: '127.0.0.1' };
dispatch('restart_started', { message: 'Applying network settings...', enabled: false, scope: 'tailscale' });
const localDisable = snap();

// 3. Remote origin but still enabled (scope -> all) -> poll, no terminal.
reset();
global.location = { hostname: '100.64.12.7' };
dispatch('restart_started', { message: 'm', enabled: true, scope: 'all' });
const remoteEnabled = snap();

// 4. Scope narrowed to Tailscale-only while on a bare LAN IP -> terminal, no poll.
reset();
global.location = { hostname: '192.168.1.5' };
dispatch('restart_started', { message: 'm', enabled: true, scope: 'tailscale' });
const lanScopeSwitch = Object.assign(snap(), { term: terminals[0] || '' });

// 5. Scope -> Tailscale-only while already ON the tailnet (100.x) -> poll.
reset();
global.location = { hostname: '100.64.12.7' };
dispatch('restart_started', { message: 'm', enabled: true, scope: 'tailscale' });
const tailnetScopeSwitch = snap();

// 6. Scope -> Tailscale-only from a MagicDNS name -> poll.
reset();
global.location = { hostname: 'machine.tailnet.ts.net' };
dispatch('restart_started', { message: 'm', enabled: true, scope: 'tailscale' });
const magicDnsScopeSwitch = snap();

out({
    handlerCount: (ws.handlers['restart_started'] || []).length,
    lockout, localDisable, remoteEnabled,
    lanScopeSwitch, tailnetScopeSwitch, magicDnsScopeSwitch,
});
""".replace("__RA_PATH__", json.dumps(str(REMOTE_ACCESS_JS)))
        result = _run_js(tmp_path, snippet, js_file=WEBSOCKET_JS)
        assert result["handlerCount"] == 1
        # Remote (tailnet) + disable: terminal only, never polls.
        assert result["lockout"] == {"terminals": 1, "modals": 0, "polled": 0}
        # Local + disable: normal overlay + poll.
        assert result["localDisable"] == {"terminals": 0, "modals": 1, "polled": 1}
        # Remote + still enabled (all): poll (the origin is still served).
        assert result["remoteEnabled"] == {"terminals": 0, "modals": 1, "polled": 1}
        # Scope narrowed to Tailscale from a bare LAN IP: terminal, no poll — the
        # SHARED rule now catches this case the old inline check missed.
        assert result["lanScopeSwitch"]["terminals"] == 1
        assert result["lanScopeSwitch"]["modals"] == 0
        assert result["lanScopeSwitch"]["polled"] == 0
        assert "will not reconnect" in result["lanScopeSwitch"]["term"]
        # Already on the tailnet, or a MagicDNS name: still covered -> poll.
        assert result["tailnetScopeSwitch"] == {"terminals": 0, "modals": 1, "polled": 1}
        assert result["magicDnsScopeSwitch"] == {"terminals": 0, "modals": 1, "polled": 1}


def test_visibilitychange_does_not_nuke_an_inflight_oauth_banner():
    """The visibilitychange handler must bail out on ``_accountActionInFlight``
    BEFORE it re-renders the route.

    Regression: a remote dashboard runs the OAuth flow in manual mode, which
    sends the user to Claude's authorization page in a SECOND TAB. Switching
    back to the dashboard fires ``visibilitychange`` mid-flow, and the handler's
    ``renderRoute()`` rebuilds the accounts view wholesale -- taking
    ``#oauth-flow-status`` with it and destroying the authorization link and the
    code paste box at the exact moment the user returns holding the code. The
    flow then polls invisibly until it times out with nowhere to paste, which
    makes adding or re-authing an account from another machine impossible.

    ``rerenderAccountsView()`` preserves that banner across a re-render;
    ``renderRoute()`` does not. So while an account action is live the handler
    must not re-render at all -- exactly what ``startPolling()`` already does
    with this same flag.

    Source-level assertion on purpose: the shared DOM stub in
    ``test_web_js_swap_ui`` makes ``document.addEventListener`` a no-op, so the
    handler is never captured and cannot be dispatched. Same approach as the
    ``window.addEventListener('storage'`` guard in the swap-ui suite.
    """
    src = WEBSOCKET_JS.read_text(encoding="utf-8")

    marker = "document.addEventListener('visibilitychange'"
    start = src.index(marker)
    # The handler is the last top-level listener in the file; bound the block at
    # the next top-level statement if one is ever added below it.
    end = src.find("\ndocument.addEventListener", start + len(marker))
    block = src[start:] if end == -1 else src[start:end]

    # Strip line comments before measuring order. The guard's own explanatory
    # comment names renderRoute(), and matching that prose instead of the call
    # inverts the result.
    code = "\n".join(
        ln for ln in block.split("\n") if not ln.strip().startswith("//")
    )

    guard = code.find("_accountActionInFlight")
    render = code.find("renderRoute(")

    assert guard != -1, (
        "visibilitychange handler lost its _accountActionInFlight guard; an "
        "in-flight OAuth code-entry banner will be destroyed when the user "
        "returns from the authorization tab"
    )
    assert render != -1, "expected the handler to re-render via renderRoute()"
    assert guard < render, (
        "the _accountActionInFlight guard must come BEFORE renderRoute(), "
        "otherwise the re-render still nukes the paste box"
    )
    # The guard has to be an early return, not just a mention.
    guard_line = code[guard:].split("\n", 1)[0]
    assert "return" in guard_line, (
        f"expected an early return on the in-flight guard, got: {guard_line!r}"
    )
