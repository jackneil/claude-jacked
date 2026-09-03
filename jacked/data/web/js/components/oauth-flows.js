/**
 * jacked web dashboard — OAuth polling flows
 * Extracted from account-actions.js for guardrails compliance.
 * Add / re-auth / CC-auth share runOAuthFlow(); each one supplies its start
 * request, banner accent, and copy. Every flow self-guards with
 * window.jackedState._accountActionInFlight.
 */

// ---------------------------------------------------------------------------
// Shared flow engine — styling, timings, and remote detection
// ---------------------------------------------------------------------------

// Terminal banners look the same for every flow; only the copy differs.
const OAUTH_SUCCESS_CLASS = 'bg-green-900/30 border border-green-700 rounded-lg px-4 py-3 text-sm text-green-200';
const OAUTH_ERROR_CLASS = 'bg-red-900/30 border border-red-700 rounded-lg px-4 py-3 text-sm text-red-200';
const OAUTH_WARN_CLASS = 'bg-yellow-900/30 border border-yellow-700 rounded-lg px-4 py-3 text-sm text-yellow-200';

// Pending-banner accents. Full class strings, not built from a color name —
// Tailwind's JIT only sees classes that appear literally in the source.
const OAUTH_ACCENT_BLUE = {
    banner: 'bg-blue-900/30 border border-blue-700 rounded-lg px-4 py-3 text-sm text-blue-200 flex items-center gap-3',
    subtitle: 'text-xs text-blue-300 mt-1',
    link: 'inline-block text-xs text-blue-300 underline hover:text-blue-200 mt-2',
};
const OAUTH_ACCENT_ORANGE = {
    banner: 'bg-orange-900/30 border border-orange-700 rounded-lg px-4 py-3 text-sm text-orange-200 flex items-center gap-3',
    subtitle: 'text-xs text-orange-300 mt-1',
    link: 'inline-block text-xs text-orange-300 underline hover:text-orange-200 mt-2',
};

const OAUTH_CODE_INPUT_CLASS = 'flex-1 min-w-0 bg-slate-900 border border-slate-600 rounded px-2 py-1 text-xs text-white placeholder-slate-500 font-mono focus:outline-none focus:border-blue-500';
const OAUTH_CODE_BUTTON_CLASS = 'px-3 py-1 bg-blue-600 hover:bg-blue-500 text-white text-xs rounded transition active:scale-[0.96] disabled:opacity-40 disabled:cursor-not-allowed';

// Manual mode: no server-side browser, no localhost callback — the user opens
// the link themselves and pastes back the code Claude shows.
const OAUTH_SUBTITLE_MANUAL = 'Click the link and approve. Claude then shows a code. Copy the code and paste it below.';
const OAUTH_SUBTITLE_BROWSER = 'A browser window should open on this machine. If it does not open, use the link.';

// Poll every 1s. Manual flows get the longer window because a human is copying
// a code between machines (mirrors MANUAL_TIMEOUT_SECONDS server-side).
const OAUTH_MANUAL_MAX_WAIT = 600;
const OAUTH_BROWSER_MAX_WAIT = 120;
const OAUTH_MAX_POLL_ERRORS = 5;

// A dashboard reached over the network can't use the server's browser or its
// loopback callback, so ask the API for a manual flow.
function isRemoteDashboard() {
    return !['localhost', '127.0.0.1', '::1', '[::1]'].includes(window.location.hostname);
}

// Pending banner: spinner plus title. Returns the text column so the caller can
// fill in the mode-dependent parts once the start response comes back.
function buildOAuthBanner(statusEl, accent, titleText) {
    statusEl.textContent = '';
    const banner = document.createElement('div');
    banner.className = accent.banner;
    const spinner = document.createElement('div');
    spinner.className = 'spinner';
    banner.appendChild(spinner);
    const textDiv = document.createElement('div');
    textDiv.className = 'min-w-0 flex-1';
    const title = document.createElement('div');
    title.className = 'font-medium';
    title.textContent = titleText;
    textDiv.appendChild(title);
    banner.appendChild(textDiv);
    statusEl.appendChild(banner);
    return textDiv;
}

// Mode copy, the authorization link, and the paste-a-code row. Code entry shows
// in both modes: it's the only way home for a manual flow, and a working
// fallback when a local browser redirect never lands.
function buildOAuthCodeEntry(textDiv, accent, authUrl, manual, identity) {
    const email = (identity && identity.email) || '';
    const orgName = (identity && identity.orgName) || '';
    const browserMode = (identity && identity.browserMode) || '';
    const browserName = (identity && identity.browserName) || '';
    const flowId = (identity && identity.flowId) || '';

    // Which account is being authorized, above the instructions: the login
    // page is now pre-filled with this email, so the banner has to say whose.
    if (email) {
        const who = document.createElement('div');
        who.className = 'text-sm font-medium text-white';
        who.textContent = 'Authorizing ' + email;
        if (orgName) {
            const org = document.createElement('span');
            org.className = 'text-slate-400 font-normal';
            org.textContent = ' · ' + orgName;
            who.appendChild(org);
        }
        textDiv.appendChild(who);
    }

    const subtitle = document.createElement('div');
    subtitle.className = accent.subtitle;
    let subtitleText = manual ? OAUTH_SUBTITLE_MANUAL : OAUTH_SUBTITLE_BROWSER;
    if (browserMode === 'profile') {
        subtitleText += ' A dedicated ' + (browserName || 'browser') + ' window opened for '
            + (email || 'this login') + '. It may be behind this window or on the taskbar.';
    } else if (browserMode === 'incognito') {
        subtitleText += ' A private ' + (browserName || 'browser')
            + ' window opened for this login. It may be behind this window or on the taskbar.';
    }
    subtitle.textContent = subtitleText;
    textDiv.appendChild(subtitle);

    // A window jacked launched itself is the only one signed in to the right
    // account. Windows refuses foreground to a window opened by a background
    // service, so the primary action is "raise that window", and the raw link
    // drops to a labelled last resort — clicking it authorizes whichever
    // account this dashboard's browser happens to be signed in to.
    const canReopen = !manual && (browserMode === 'profile' || browserMode === 'incognito') && flowId;

    if (canReopen) {
        const reopenRow = document.createElement('div');
        reopenRow.className = 'mt-2';
        const reopenBtn = document.createElement('button');
        reopenBtn.type = 'button';
        reopenBtn.className = OAUTH_CODE_BUTTON_CLASS;
        reopenBtn.textContent = 'Bring up the sign-in window';
        reopenBtn.addEventListener('click', async () => {
            reopenBtn.disabled = true;
            try {
                const result = await api.post('/api/auth/flow/' + flowId + '/open');
                if (result && result.reopen_error) showToast(result.reopen_error, 'warning');
            } catch (e) {
                showToast('Could not reopen the sign-in window: ' + e.message, 'error');
            } finally {
                reopenBtn.disabled = false;
            }
        });
        reopenRow.appendChild(reopenBtn);
        textDiv.appendChild(reopenRow);
    }

    if (authUrl && canReopen) {
        const fallback = document.createElement('div');
        fallback.className = 'text-xs text-slate-400 mt-1';
        const link = document.createElement('a');
        link.className = accent.link;
        link.textContent = 'Open in this browser instead';
        link.target = '_blank';
        link.rel = 'noopener';
        link.href = authUrl;
        fallback.appendChild(link);
        fallback.appendChild(
            document.createTextNode(' (uses whatever account this browser is already signed in to)')
        );
        textDiv.appendChild(fallback);
    } else if (authUrl) {
        const link = document.createElement('a');
        link.className = accent.link;
        link.textContent = email
            ? 'Open the authorization page for ' + email
            : 'Open the authorization page';
        link.target = '_blank';
        link.rel = 'noopener';
        link.href = authUrl;
        textDiv.appendChild(link);
    }

    const codeRow = document.createElement('div');
    codeRow.className = 'flex items-center gap-2 mt-2';
    const codeInput = document.createElement('input');
    codeInput.type = 'text';
    codeInput.name = 'oauth-authorization-code';
    codeInput.className = OAUTH_CODE_INPUT_CLASS;
    codeInput.placeholder = 'Paste the authorization code';
    codeInput.autocomplete = 'off';
    codeInput.spellcheck = false;
    const submitBtn = document.createElement('button');
    submitBtn.type = 'button';
    submitBtn.className = OAUTH_CODE_BUTTON_CLASS;
    submitBtn.textContent = 'Submit code';
    codeRow.appendChild(codeInput);
    codeRow.appendChild(submitBtn);
    textDiv.appendChild(codeRow);

    const submitError = document.createElement('div');
    submitError.className = 'text-xs text-red-300 mt-1';
    submitError.hidden = true;
    textDiv.appendChild(submitError);

    return { codeInput, submitBtn, submitError };
}

/**
 * Run one OAuth banner flow start to finish.
 *
 * opts: {
 *   startPath  — POST path that starts the flow (remote=true appended when needed)
 *   title      — banner headline
 *   accent     — OAUTH_ACCENT_BLUE | OAUTH_ACCENT_ORANGE
 *   messages   — { startFailPrefix, failPrefix, timedOut(wait), notFound,
 *                  expired, checkFailed, success(poll) -> { text, duration } }
 * }
 */
async function runOAuthFlow(opts) {
    // If a previous OAuth flow is still polling, cancel it and start fresh.
    // The user clicking again means they want a new browser window.
    if (window.jackedState.flowPolling) {
        clearInterval(window.jackedState.flowPolling);
        window.jackedState.flowPolling = null;
    }
    window.jackedState._accountActionInFlight = true;

    const statusEl = document.getElementById('oauth-flow-status');
    if (!statusEl) {
        window.jackedState._accountActionInFlight = false;
        return;
    }

    const msgs = opts.messages;
    let pollTimer = null;
    // The poller and a code submission race to finish the flow. First one to
    // reach a terminal state wins; the loser must not re-render or double-refresh.
    let terminal = false;

    // Every banner is built node by node — no innerHTML with interpolated data.
    // Always re-look-up the slot: the accounts view re-renders wholesale
    // (content.innerHTML = renderAccounts(...)), so a held reference can point
    // at a detached node and the message would render invisibly.
    function renderBanner(className, text, clearAfterMs) {
        const slot = document.getElementById('oauth-flow-status') || statusEl;
        slot.textContent = '';
        const div = document.createElement('div');
        div.className = className;
        div.textContent = text;
        slot.appendChild(div);
        // Clear only our own node — a newer flow may own the slot by then.
        if (clearAfterMs) setTimeout(() => { if (div.isConnected) div.remove(); }, clearAfterMs);
    }

    function stopPolling() {
        if (pollTimer !== null) clearInterval(pollTimer);
        // Only release the shared slot if it's still ours — a newer flow may
        // have claimed it while a submit was in flight.
        if (window.jackedState.flowPolling === pollTimer) window.jackedState.flowPolling = null;
        pollTimer = null;
        document.removeEventListener('visibilitychange', pollOnVisible);
        window.removeEventListener('focus', pollOnVisible);
    }

    // End the flow on a local verdict: timeout, expiry, cancel, or a dead poll loop.
    function endWith(className, text) {
        if (terminal) return;
        terminal = true;
        stopPolling();
        renderBanner(className, text);
        window.jackedState._accountActionInFlight = false;
    }

    // Spinner and title go up immediately; the mode-dependent parts wait for
    // the start response to say which mode we got.
    const textDiv = buildOAuthBanner(statusEl, opts.accent, opts.title);

    // A way out that is not "reload the page". While a flow is pending, the
    // shared guard refuses every other account action (Use Account included)
    // with nothing but a short toast, so a sign-in window that was closed, or
    // a verdict that never shows up, used to leave the dashboard looking dead
    // until the flow timed out or the page was reloaded. Cancelling settles
    // the banner locally; a sign-in the browser already finished is stored
    // server-side regardless, and the refresh right after picks it up.
    const bannerEl = textDiv.parentNode;
    if (bannerEl) {
        const cancelBtn = document.createElement('button');
        cancelBtn.type = 'button';
        cancelBtn.className = 'shrink-0 self-start text-xs text-slate-400 hover:text-white underline';
        cancelBtn.textContent = 'Cancel';
        cancelBtn.setAttribute('data-oauth-cancel', 'true');
        cancelBtn.addEventListener('click', () => {
            endWith(OAUTH_WARN_CLASS, 'Sign-in cancelled. If you already approved it in the browser, the account updates on the next refresh.');
            if (typeof loadAllData === 'function' && typeof rerenderAccountsView === 'function') {
                loadAllData().then(() => rerenderAccountsView()).catch(() => {});
            }
        });
        bannerEl.appendChild(cancelBtn);
    }

    let start;
    try {
        const suffix = opts.startPath.includes('?') ? '&remote=true' : '?remote=true';
        start = await api.post(opts.startPath + (isRemoteDashboard() ? suffix : ''));
    } catch (e) {
        endWith(OAUTH_ERROR_CLASS, msgs.startFailPrefix + e.message);
        return;
    }

    const flowId = start.flow_id;
    if (!flowId) {
        endWith(OAUTH_ERROR_CLASS, 'No flow ID returned from server');
        return;
    }

    const manual = start.mode === 'manual';
    const maxWait = manual ? OAUTH_MANUAL_MAX_WAIT : OAUTH_BROWSER_MAX_WAIT;
    const waitLabel = Math.round(maxWait / 60) + ' minutes';

    const { codeInput, submitBtn, submitError } =
        buildOAuthCodeEntry(textDiv, opts.accent, start.auth_url, manual, {
            email: start.target_email,
            orgName: start.target_org_name,
            browserMode: start.browser_mode,
            browserName: start.browser_name,
            flowId: flowId,
        });

    // Server verdict, shared by the poller and the code submission.
    // Returns true once the flow is done and the banner has been replaced.
    async function handleFlowResult(poll) {
        if (!['completed', 'error', 'not_found'].includes(poll.status)) {
            return false;  // status === 'pending' — keep polling
        }
        if (terminal) return true;

        if (poll.status === 'completed') {
            terminal = true;
            stopPolling();
            // The server's verdict is what the guard was waiting on, so drop
            // it now, not after the refresh: refreshAndRender fetches every
            // account (and, on macOS, reconciles the active one through the
            // Keychain), and a Use Account click during that window used to
            // be refused as "another action in progress".
            window.jackedState._accountActionInFlight = false;
            const success = msgs.success(poll);
            // Refresh FIRST: refreshAndRender re-renders the route wholesale,
            // which would wipe a banner drawn before it. Render the success
            // message into the fresh slot afterwards.
            await refreshAndRender();
            renderBanner(OAUTH_SUCCESS_CLASS, success.text, success.duration);
        } else if (poll.status === 'error') {
            endWith(OAUTH_ERROR_CLASS, msgs.failPrefix + (poll.error || 'Unknown error'));
        } else {
            endWith(OAUTH_WARN_CLASS, msgs.notFound);
        }
        return true;
    }

    async function submitCode() {
        submitError.hidden = true;
        submitBtn.disabled = true;
        try {
            const result = await api.post(`/api/auth/flow/${flowId}/code`, { code: codeInput.value });
            if (await handleFlowResult(result)) return;
            // Recoverable paste problem: stay pending and let the user retry.
            submitError.textContent = result.submit_error || 'That code was not accepted. Please try again.';
            submitError.hidden = false;
        } catch (e) {
            if (e.status === 404) {
                endWith(OAUTH_WARN_CLASS, msgs.expired);
                return;
            }
            submitError.textContent = e.message || 'Could not submit the code.';
            submitError.hidden = false;
        } finally {
            submitBtn.disabled = false;
        }
    }

    submitBtn.addEventListener('click', submitCode);
    codeInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            submitCode();
        }
    });

    // One poll of the server verdict. Shared by the interval and the
    // visibility hook; overlapping calls collapse into the one in flight.
    let consecutiveErrors = 0;
    let pollInFlight = false;
    async function pollOnce() {
        if (terminal || pollInFlight) return;
        pollInFlight = true;
        try {
            const poll = await api.get(`/api/auth/flow/${flowId}`);
            consecutiveErrors = 0;
            await handleFlowResult(poll);
        } catch (e) {
            if (e.status === 404) {
                endWith(OAUTH_WARN_CLASS, msgs.expired);
            } else {
                consecutiveErrors++;
                if (consecutiveErrors >= OAUTH_MAX_POLL_ERRORS) {
                    endWith(OAUTH_ERROR_CLASS, msgs.checkFailed);
                }
            }
        } finally {
            pollInFlight = false;
        }
    }

    // The sign-in window takes the foreground, and a hidden tab's timers are
    // throttled or frozen, so the interval alone can leave the verdict unread
    // for minutes after the callback landed. Coming back to the tab polls
    // straight away instead.
    function pollOnVisible() {
        if (document.hidden) return;
        pollOnce();
    }

    let elapsed = 0;
    pollTimer = setInterval(() => {
        elapsed++;
        if (elapsed > maxWait) {
            endWith(OAUTH_WARN_CLASS, msgs.timedOut(waitLabel));
            return;
        }
        pollOnce();
    }, 1000);

    window.jackedState.flowPolling = pollTimer;
    document.addEventListener('visibilitychange', pollOnVisible);
    window.addEventListener('focus', pollOnVisible);
}

// ---------------------------------------------------------------------------
// OAuth add-account flow
// ---------------------------------------------------------------------------
async function startAddAccountFlow() {
    return runOAuthFlow({
        startPath: '/api/auth/accounts/add',
        title: 'Waiting for authorization...',
        accent: OAUTH_ACCENT_BLUE,
        messages: {
            startFailPrefix: 'Failed to start auth flow: ',
            failPrefix: 'Authorization failed: ',
            timedOut: (wait) => `Authorization timed out after ${wait}. Please try again.`,
            notFound: 'Authorization flow not found - it may have expired. Please try again.',
            expired: 'Authorization flow expired. Please try again.',
            checkFailed: 'Authorization check failed repeatedly. Please try again.',
            // Show the org-redirect notice longer — the user picked one org and
            // authorized another, and that's worth reading.
            success: (poll) => {
                const acctEmail = poll.email || '';
                const orgName = poll.organization_name || '';
                if (poll.redirected_from_account_id) {
                    return {
                        text: 'Updated ' + acctEmail + (orgName ? ' (' + orgName + ')' : '')
                            + ' - you authorized a different org than selected',
                        duration: 10000,
                    };
                }
                return {
                    text: acctEmail ? acctEmail + ' connected successfully!' : 'Account connected successfully!',
                    duration: 3000,
                };
            },
        },
    });
}

// ---------------------------------------------------------------------------
// OAuth re-auth flow (targets existing account by ID)
// ---------------------------------------------------------------------------
async function startReauthFlow(accountId, email) {
    return runOAuthFlow({
        startPath: '/api/auth/accounts/' + accountId + '/reauth',
        title: 'Re-authenticating ' + email + '...',
        accent: OAUTH_ACCENT_BLUE,
        messages: {
            startFailPrefix: 'Failed to start re-auth flow: ',
            failPrefix: 'Re-authentication failed: ',
            timedOut: (wait) => `Re-authentication timed out after ${wait}. Please try again.`,
            notFound: 'Re-auth flow not found - it may have expired. Please try again.',
            expired: 'Re-auth flow expired. Please try again.',
            checkFailed: 'Re-auth check failed repeatedly. Please try again.',
            success: () => ({ text: 'Account re-authenticated successfully!', duration: 3000 }),
        },
    });
}

// ---------------------------------------------------------------------------
// CC token authorization flow
// ---------------------------------------------------------------------------
async function startCcAuthFlow(accountId, email) {
    return runOAuthFlow({
        startPath: `/api/auth/accounts/${accountId}/authorize-cc`,
        title: `Authorizing CC token for ${email}...`,
        accent: OAUTH_ACCENT_ORANGE,
        messages: {
            startFailPrefix: 'CC auth failed: ',
            failPrefix: 'CC authorization failed: ',
            timedOut: (wait) => `CC authorization timed out after ${wait}. Please try again.`,
            notFound: 'CC authorization flow not found - it may have expired. Please try again.',
            expired: 'CC authorization flow expired. Please try again.',
            checkFailed: 'CC authorization check failed repeatedly. Please try again.',
            success: () => ({ text: 'CC token authorized successfully!', duration: 3000 }),
        },
    });
}

// ---------------------------------------------------------------------------
// Codex add flow — imports the signed-in ~/.codex account (no browser OAuth;
// Codex sign-in happens via `codex login` in a terminal).
// ---------------------------------------------------------------------------
async function startAddCodexFlow() {
    window.jackedState._accountActionInFlight = true;
    const statusEl = document.getElementById('oauth-flow-status');
    if (statusEl) {
        statusEl.innerHTML = `
            <div class="bg-blue-900/30 border border-blue-700 rounded-lg px-4 py-3 text-sm text-blue-200 flex items-center gap-3">
                <div class="spinner"></div>
                <div>Importing your signed-in Codex account…</div>
            </div>`;
    }
    try {
        const result = await api.post('/api/auth/accounts/add?provider=codex');
        if (statusEl) statusEl.innerHTML = '';
        showToast(`Added Codex account ${result.email || ''}`.trim(), 'success');
        await refreshAndRender();
    } catch (e) {
        const needsLogin = e && e.code === 'CODEX_LOGIN_REQUIRED';
        const msg = needsLogin
            ? 'Not signed in to Codex. Run `codex login` in a terminal, then click Add Account → Codex again.'
            : (e && e.message) || 'Failed to add Codex account';
        if (statusEl) {
            statusEl.innerHTML = `
                <div class="bg-amber-900/30 border border-amber-700 rounded-lg px-4 py-3 text-sm text-amber-200">
                    ${escapeHtml(msg)}
                </div>`;
        } else {
            showToast(msg, needsLogin ? 'warning' : 'error');
        }
    } finally {
        window.jackedState._accountActionInFlight = false;
    }
}
