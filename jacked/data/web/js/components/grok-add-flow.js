/**
 * jacked web dashboard: the Add Account > Grok flow and the Grok Re-auth row.
 *
 * Loaded by index.html after oauth-flows.js and before account-actions.js. It
 * reads names from other scripts only at call time, never at load time:
 * buildOAuthBanner and OAUTH_ACCENT_BLUE (oauth-flows.js), and api,
 * showToast and refreshAndRender (app.js).
 *
 * Grok keeps its own login. Add imports the account that `grok login`
 * signed in on the computer that runs jacked. When nobody is signed in, the
 * server opens the Grok sign-in in a browser profile, and the user adds the
 * account again after the sign-in. Re-auth opens that sign-in for one
 * account. The messages come from the server, so the banner is built node by
 * node and never reaches innerHTML.
 */

const GROK_NOTICE_CLASS = 'bg-amber-900/30 border border-amber-700 rounded-lg px-4 py-3 text-sm text-amber-200';

function showGrokNotice(message) {
    // Re-look-up the slot: the accounts view can re-render while the request runs.
    const slot = document.getElementById('oauth-flow-status');
    if (!slot) {
        showToast(message, 'warning', 10000);
        return;
    }
    slot.textContent = '';
    const banner = document.createElement('div');
    banner.className = GROK_NOTICE_CLASS;
    banner.appendChild(document.createTextNode(message));
    slot.appendChild(banner);
}

async function _runGrokRequest(path, busyText, onImported) {
    window.jackedState._accountActionInFlight = true;
    const statusEl = document.getElementById('oauth-flow-status');
    if (statusEl) buildOAuthBanner(statusEl, OAUTH_ACCENT_BLUE, busyText);
    try {
        const result = await api.post(path);
        if (result && result.profile_login) {
            showGrokNotice(result.message || 'The Grok sign-in opened in a browser profile.');
            return;
        }
        const slot = document.getElementById('oauth-flow-status');
        if (slot) slot.textContent = '';
        await onImported(result || {});
    } catch (e) {
        showGrokNotice((e && e.message) || 'jacked could not start the Grok sign-in.');
    } finally {
        window.jackedState._accountActionInFlight = false;
    }
}

async function startAddGrokFlow() {
    return _runGrokRequest(
        '/api/auth/accounts/add?provider=grok',
        'Checking your signed-in Grok account…',
        async (result) => {
            showToast(`Added Grok account ${result.email || ''}`.trim(), 'success');
            await refreshAndRender();
        },
    );
}

async function startGrokReauthFlow(id, email) {
    return _runGrokRequest(
        `/api/auth/accounts/${encodeURIComponent(id)}/reauth`,
        `Opening the Grok sign-in for ${email || 'this account'}…`,
        async () => { await refreshAndRender(); },
    );
}
