/**
 * jacked web dashboard: the Add Account > Codex flow.
 *
 * Moved out of oauth-flows.js, which is over the file-size limit. Loaded by
 * index.html right after oauth-flows.js. It reads names from other scripts
 * only at call time, never at load time: buildOAuthBanner, OAUTH_ACCENT_BLUE
 * and isRemoteDashboard (oauth-flows.js), and api, showToast and
 * refreshAndRender (app.js). account-actions.js calls startAddCodexFlow.
 *
 * Codex has no browser OAuth that jacked drives: the add imports the account
 * that `codex login` signed in on the computer that runs jacked. A refusal
 * names its own fix, by the route's error code:
 * - CODEX_LOGIN_REQUIRED: no usable sign-in. Run `codex login`.
 * - CODEX_STORE_UNSWITCHABLE: Codex keeps the credential in the OS keyring
 *   ("keyring", or "auto" with no usable auth.json). A login alone writes the
 *   keyring again and the next add is refused again, so the store must be
 *   set to "file" first.
 * Both fixes happen on the computer that runs jacked, which is not this
 * computer when the dashboard is remote. The reason comes from the server,
 * so the banner is built node by node and never reaches innerHTML.
 */

const CODEX_ADD_REFUSED_CLASS = 'bg-amber-900/30 border border-amber-700 rounded-lg px-4 py-3 text-sm text-amber-200 space-y-1';
const CODEX_ADD_CODE_CLASS = 'font-mono text-amber-100';
// The last sentences of both fixes: open the add dialog again.
const CODEX_ADD_RETRY = [' Click Add Account. In the dialog, click Codex.'];

function codexFixLocation(remote) {
    return remote
        ? 'Do these steps on the computer that runs jacked, not on this computer.'
        : 'Do these steps on this computer.';
}

// The banner lines, in reading order. A line is a list of parts: a string is
// text, { code } is a command or a setting.
function codexAddRefusalLines(error, remote) {
    const code = error && error.code;
    const where = codexFixLocation(remote);
    if (code === 'CODEX_STORE_UNSWITCHABLE') {
        return [
            [error.message],
            [where + ' Set ', { code: 'cli_auth_credentials_store = "file"' }, ' in ',
             { code: '~/.codex/config.toml' }, '. Run ', { code: 'codex login' },
             ' in a terminal.'].concat(CODEX_ADD_RETRY),
        ];
    }
    if (code === 'CODEX_LOGIN_REQUIRED') {
        return [
            ['jacked cannot find a Codex sign-in that it can use.'],
            [where + ' Run ', { code: 'codex login' }, ' in a terminal.'].concat(CODEX_ADD_RETRY),
        ];
    }
    return [[(error && error.message) || 'jacked could not add the Codex account.']];
}

function codexCopyText(parts) {
    return parts.map(part => (typeof part === 'string' ? part : part.code)).join('');
}

function codexCopyLine(parts) {
    const line = document.createElement('div');
    parts.forEach(part => {
        if (typeof part === 'string') {
            line.appendChild(document.createTextNode(part));
            return;
        }
        const code = document.createElement('code');
        code.className = CODEX_ADD_CODE_CLASS;
        code.textContent = part.code;
        line.appendChild(code);
    });
    return line;
}

function showCodexAddRefusal(error) {
    const lines = codexAddRefusalLines(error, isRemoteDashboard());
    const known = error && (error.code === 'CODEX_STORE_UNSWITCHABLE' || error.code === 'CODEX_LOGIN_REQUIRED');
    // Re-look-up the slot: the accounts view can re-render while the request runs.
    const slot = document.getElementById('oauth-flow-status');
    if (!slot) {
        showToast(lines.map(codexCopyText).join(' '), known ? 'warning' : 'error', 10000);
        return;
    }
    slot.textContent = '';
    const banner = document.createElement('div');
    banner.className = CODEX_ADD_REFUSED_CLASS;
    lines.forEach(parts => banner.appendChild(codexCopyLine(parts)));
    slot.appendChild(banner);
}

async function startAddCodexFlow() {
    window.jackedState._accountActionInFlight = true;
    const statusEl = document.getElementById('oauth-flow-status');
    if (statusEl) buildOAuthBanner(statusEl, OAUTH_ACCENT_BLUE, 'Importing your signed-in Codex account…');
    try {
        const result = await api.post('/api/auth/accounts/add?provider=codex');
        const slot = document.getElementById('oauth-flow-status');
        if (slot) slot.textContent = '';
        showToast(`Added Codex account ${result.email || ''}`.trim(), 'success');
        await refreshAndRender();
    } catch (e) {
        showCodexAddRefusal(e);
    } finally {
        window.jackedState._accountActionInFlight = false;
    }
}
