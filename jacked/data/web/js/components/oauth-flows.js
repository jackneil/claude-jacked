/**
 * jacked web dashboard — OAuth polling flows
 * Extracted from account-actions.js for guardrails compliance.
 * Both flows self-guard with window.jackedState._accountActionInFlight.
 */

// ---------------------------------------------------------------------------
// OAuth add-account flow
// ---------------------------------------------------------------------------
async function startAddAccountFlow() {
    if (window.jackedState._accountActionInFlight) return;
    window.jackedState._accountActionInFlight = true;

    const statusEl = document.getElementById('oauth-flow-status');
    if (!statusEl) {
        window.jackedState._accountActionInFlight = false;
        return;
    }

    statusEl.innerHTML = `
        <div class="bg-blue-900/30 border border-blue-700 rounded-lg px-4 py-3 text-sm text-blue-200 flex items-center gap-3">
            <div class="spinner"></div>
            <div>
                <div class="font-medium">Waiting for authorization...</div>
                <div class="text-xs text-blue-300 mt-1">A browser window should open. Complete the authorization there.</div>
            </div>
        </div>
    `;

    let flowId;
    try {
        const result = await api.post('/api/auth/accounts/add');
        flowId = result.flow_id;
    } catch (e) {
        statusEl.innerHTML = `
            <div class="bg-red-900/30 border border-red-700 rounded-lg px-4 py-3 text-sm text-red-200">
                Failed to start auth flow: ${escapeHtml(e.message)}
            </div>
        `;
        window.jackedState._accountActionInFlight = false;
        return;
    }

    if (!flowId) {
        statusEl.innerHTML = `
            <div class="bg-red-900/30 border border-red-700 rounded-lg px-4 py-3 text-sm text-red-200">
                No flow ID returned from server
            </div>
        `;
        window.jackedState._accountActionInFlight = false;
        return;
    }

    // Poll every 1s, timeout at 2 minutes
    let elapsed = 0;
    let consecutiveErrors = 0;
    const maxWait = 120;
    const maxErrors = 5;
    const pollInterval = setInterval(async () => {
        elapsed++;
        if (elapsed > maxWait) {
            clearInterval(pollInterval);
            window.jackedState.flowPolling = null;
            statusEl.innerHTML = `
                <div class="bg-yellow-900/30 border border-yellow-700 rounded-lg px-4 py-3 text-sm text-yellow-200">
                    Authorization timed out after 2 minutes. Please try again.
                </div>
            `;
            window.jackedState._accountActionInFlight = false;
            return;
        }

        try {
            const poll = await api.get(`/api/auth/flow/${flowId}`);
            consecutiveErrors = 0;

            if (poll.status === 'completed') {
                clearInterval(pollInterval);
                window.jackedState.flowPolling = null;
                statusEl.innerHTML = `
                    <div class="bg-green-900/30 border border-green-700 rounded-lg px-4 py-3 text-sm text-green-200">
                        Account connected successfully!
                    </div>
                `;
                setTimeout(() => { statusEl.innerHTML = ''; }, 3000);
                try {
                    await refreshAndRender();
                } finally {
                    window.jackedState._accountActionInFlight = false;
                }
            } else if (poll.status === 'error') {
                clearInterval(pollInterval);
                window.jackedState.flowPolling = null;
                statusEl.innerHTML = `
                    <div class="bg-red-900/30 border border-red-700 rounded-lg px-4 py-3 text-sm text-red-200">
                        Authorization failed: ${escapeHtml(poll.error || 'Unknown error')}
                    </div>
                `;
                window.jackedState._accountActionInFlight = false;
            } else if (poll.status === 'not_found') {
                clearInterval(pollInterval);
                window.jackedState.flowPolling = null;
                statusEl.innerHTML = `
                    <div class="bg-yellow-900/30 border border-yellow-700 rounded-lg px-4 py-3 text-sm text-yellow-200">
                        Authorization flow not found — it may have expired. Please try again.
                    </div>
                `;
                window.jackedState._accountActionInFlight = false;
            }
            // status === 'pending' — keep polling
        } catch (e) {
            if (e.status === 404) {
                clearInterval(pollInterval);
                window.jackedState.flowPolling = null;
                statusEl.innerHTML = `
                    <div class="bg-yellow-900/30 border border-yellow-700 rounded-lg px-4 py-3 text-sm text-yellow-200">
                        Authorization flow expired. Please try again.
                    </div>
                `;
                window.jackedState._accountActionInFlight = false;
            } else {
                consecutiveErrors++;
                if (consecutiveErrors >= maxErrors) {
                    clearInterval(pollInterval);
                    window.jackedState.flowPolling = null;
                    statusEl.innerHTML = `
                        <div class="bg-red-900/30 border border-red-700 rounded-lg px-4 py-3 text-sm text-red-200">
                            Authorization check failed repeatedly. Please try again.
                        </div>
                    `;
                    window.jackedState._accountActionInFlight = false;
                }
            }
        }
    }, 1000);

    window.jackedState.flowPolling = pollInterval;
}

// ---------------------------------------------------------------------------
// CC token authorization flow
// ---------------------------------------------------------------------------
async function startCcAuthFlow(accountId, email) {
    if (window.jackedState._accountActionInFlight) return;
    window.jackedState._accountActionInFlight = true;

    const statusEl = document.getElementById('oauth-flow-status');
    if (!statusEl) {
        window.jackedState._accountActionInFlight = false;
        return;
    }

    statusEl.textContent = '';
    const banner = document.createElement('div');
    banner.className = 'bg-orange-900/30 border border-orange-700 rounded-lg px-4 py-3 text-sm text-orange-200 flex items-center gap-3';
    const spinner = document.createElement('div');
    spinner.className = 'spinner';
    banner.appendChild(spinner);
    const textDiv = document.createElement('div');
    const title = document.createElement('div');
    title.className = 'font-medium';
    title.textContent = `Authorizing CC token for ${email}...`;
    textDiv.appendChild(title);
    const subtitle = document.createElement('div');
    subtitle.className = 'text-xs text-orange-300 mt-1';
    subtitle.textContent = 'A browser window should open. Sign in with the same Google account.';
    textDiv.appendChild(subtitle);
    banner.appendChild(textDiv);
    statusEl.appendChild(banner);

    let flowId;
    try {
        const result = await api.post(`/api/auth/accounts/${accountId}/authorize-cc`);
        flowId = result.flow_id;
    } catch (e) {
        statusEl.textContent = '';
        const errDiv = document.createElement('div');
        errDiv.className = 'bg-red-900/30 border border-red-700 rounded-lg px-4 py-3 text-sm text-red-200';
        errDiv.textContent = `CC auth failed: ${e.message}`;
        statusEl.appendChild(errDiv);
        window.jackedState._accountActionInFlight = false;
        return;
    }

    if (!flowId) {
        statusEl.textContent = '';
        const errDiv = document.createElement('div');
        errDiv.className = 'bg-red-900/30 border border-red-700 rounded-lg px-4 py-3 text-sm text-red-200';
        errDiv.textContent = 'No flow ID returned from server';
        statusEl.appendChild(errDiv);
        window.jackedState._accountActionInFlight = false;
        return;
    }

    let elapsed = 0;
    let consecutiveErrors = 0;
    const maxWait = 120;
    const maxErrors = 5;
    const pollInterval = setInterval(async () => {
        elapsed++;
        if (elapsed > maxWait) {
            clearInterval(pollInterval);
            window.jackedState.flowPolling = null;
            statusEl.textContent = '';
            const warnDiv = document.createElement('div');
            warnDiv.className = 'bg-yellow-900/30 border border-yellow-700 rounded-lg px-4 py-3 text-sm text-yellow-200';
            warnDiv.textContent = 'CC authorization timed out after 2 minutes. Please try again.';
            statusEl.appendChild(warnDiv);
            window.jackedState._accountActionInFlight = false;
            return;
        }

        try {
            const poll = await api.get(`/api/auth/flow/${flowId}`);
            consecutiveErrors = 0;
            if (poll.status === 'completed') {
                clearInterval(pollInterval);
                window.jackedState.flowPolling = null;
                statusEl.textContent = '';
                const okDiv = document.createElement('div');
                okDiv.className = 'bg-green-900/30 border border-green-700 rounded-lg px-4 py-3 text-sm text-green-200';
                okDiv.textContent = 'CC token authorized successfully!';
                statusEl.appendChild(okDiv);
                setTimeout(() => { statusEl.textContent = ''; }, 3000);
                try {
                    await refreshAndRender();
                } finally {
                    window.jackedState._accountActionInFlight = false;
                }
            } else if (poll.status === 'error') {
                clearInterval(pollInterval);
                window.jackedState.flowPolling = null;
                statusEl.textContent = '';
                const errDiv = document.createElement('div');
                errDiv.className = 'bg-red-900/30 border border-red-700 rounded-lg px-4 py-3 text-sm text-red-200';
                errDiv.textContent = `CC authorization failed: ${poll.error || 'Unknown error'}`;
                statusEl.appendChild(errDiv);
                window.jackedState._accountActionInFlight = false;
            } else if (poll.status === 'not_found') {
                clearInterval(pollInterval);
                window.jackedState.flowPolling = null;
                statusEl.textContent = '';
                const warnDiv = document.createElement('div');
                warnDiv.className = 'bg-yellow-900/30 border border-yellow-700 rounded-lg px-4 py-3 text-sm text-yellow-200';
                warnDiv.textContent = 'CC authorization flow not found — it may have expired. Please try again.';
                statusEl.appendChild(warnDiv);
                window.jackedState._accountActionInFlight = false;
            }
        } catch (e) {
            if (e.status === 404) {
                clearInterval(pollInterval);
                window.jackedState.flowPolling = null;
                statusEl.textContent = '';
                const warnDiv = document.createElement('div');
                warnDiv.className = 'bg-yellow-900/30 border border-yellow-700 rounded-lg px-4 py-3 text-sm text-yellow-200';
                warnDiv.textContent = 'CC authorization flow expired. Please try again.';
                statusEl.appendChild(warnDiv);
                window.jackedState._accountActionInFlight = false;
            } else {
                consecutiveErrors++;
                if (consecutiveErrors >= maxErrors) {
                    clearInterval(pollInterval);
                    window.jackedState.flowPolling = null;
                    statusEl.textContent = '';
                    const errDiv = document.createElement('div');
                    errDiv.className = 'bg-red-900/30 border border-red-700 rounded-lg px-4 py-3 text-sm text-red-200';
                    errDiv.textContent = 'CC authorization check failed repeatedly. Please try again.';
                    statusEl.appendChild(errDiv);
                    window.jackedState._accountActionInFlight = false;
                }
            }
        }
    }, 1000);

    window.jackedState.flowPolling = pollInterval;
}
