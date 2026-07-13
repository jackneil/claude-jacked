/**
 * jacked web dashboard — header / version display
 */

function _timeAgo(isoStr) {
    if (!isoStr) return null;
    const diff = (Date.now() - new Date(isoStr).getTime()) / 1000;
    if (diff < 60) return 'just now';
    if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.round(diff / 3600)}h ago`;
    return `${Math.round(diff / 86400)}d ago`;
}

function _timeUntil(isoStr) {
    if (!isoStr) return null;
    const diff = (new Date(isoStr).getTime() - Date.now()) / 1000;
    if (diff <= 0) return 'soon';
    if (diff < 3600) return `in ${Math.round(diff / 60)}m`;
    if (diff < 86400) return `in ${Math.round(diff / 3600)}h`;
    return `in ${Math.round(diff / 86400)}d`;
}

function _buildTooltip(current, latest, outdated, ahead, checkedAt, nextCheckAt) {
    const lines = [];
    if (outdated && latest) {
        lines.push(`Update available: v${current} \u2192 v${latest}`);
        lines.push('uv tool upgrade claude-jacked');
    } else if (ahead && latest) {
        lines.push(`v${current} — ahead of PyPI (v${latest})`);
        lines.push('Running unreleased build');
    } else if (latest) {
        lines.push(`v${current} \u2014 latest`);
    } else {
        lines.push('Could not reach PyPI');
    }
    const ago = _timeAgo(checkedAt);
    if (ago) lines.push(`Checked: ${ago}`);
    const until = _timeUntil(nextCheckAt);
    if (until) lines.push(`Next check: ${until}`);
    return lines.join('\n');
}

async function _refreshVersion() {
    const btn = document.getElementById('version-refresh-btn');
    if (!btn || btn.classList.contains('version-refresh--spinning')) return;
    btn.classList.add('version-refresh--spinning');
    try {
        const data = await api.post('/api/version/refresh');
        window.jackedState.version = data;
        updateVersionDisplay(data);
    } catch (e) {
        console.error('Version refresh failed:', e);
    } finally {
        btn.classList.remove('version-refresh--spinning');
    }
}

async function startUpgrade() {
    const btn = document.getElementById('version-upgrade-btn');
    if (btn) btn.disabled = true;
    try {
        await api.post('/api/upgrade');
    } catch (e) {
        if (e.status === 409) return; // already in progress — WS handles UI
        _showUpgradeError('Failed to start upgrade: ' + e.message);
    }
}

function _getOrCreateUpgradeModal() {
    let modal = document.getElementById('upgrade-modal');
    if (!modal) {
        modal = document.createElement('div');
        modal.id = 'upgrade-modal';
        modal.className = 'upgrade-modal';

        const box = document.createElement('div');
        box.className = 'upgrade-modal__box';

        const spinner = document.createElement('div');
        spinner.id = 'upgrade-modal-spinner';
        spinner.className = 'spinner upgrade-modal__spinner';

        const msg = document.createElement('div');
        msg.id = 'upgrade-modal-msg';
        msg.className = 'upgrade-modal__msg';

        box.appendChild(spinner);
        box.appendChild(msg);
        modal.appendChild(box);
        document.body.appendChild(modal);
    }
    return modal;
}

function _showUpgradeModal(message) {
    const modal = _getOrCreateUpgradeModal();
    modal.style.display = 'flex';
    // A pollable/reconnecting modal: clear any terminal marker so the WS
    // disconnect handler resumes health polling for this one.
    delete modal.dataset.terminal;
    const spinner = document.getElementById('upgrade-modal-spinner');
    if (spinner) spinner.style.display = '';
    const msg = document.getElementById('upgrade-modal-msg');
    if (msg) { msg.textContent = message; msg.className = 'upgrade-modal__msg'; }
    const dismiss = modal.querySelector('.upgrade-modal__dismiss');
    if (dismiss) dismiss.remove();
}

function _updateUpgradeModal(message) {
    const el = document.getElementById('upgrade-modal-msg');
    if (el) el.textContent = message;
    else _showUpgradeModal(message);
}

function _showUpgradeError(message) {
    const modal = _getOrCreateUpgradeModal();
    modal.style.display = 'flex';
    const spinner = document.getElementById('upgrade-modal-spinner');
    if (spinner) spinner.style.display = 'none';
    const msg = document.getElementById('upgrade-modal-msg');
    if (msg) { msg.textContent = message; msg.className = 'upgrade-modal__msg upgrade-modal__msg--error'; }
    if (!modal.querySelector('.upgrade-modal__dismiss')) {
        const btn = document.createElement('button');
        btn.className = 'upgrade-modal__dismiss';
        btn.textContent = 'Dismiss';
        btn.addEventListener('click', () => modal.remove());
        modal.querySelector('.upgrade-modal__box').appendChild(btn);
    }
}

// Terminal restart state: show the modal with a final message and NO spinner
// and NO health polling. Used when the current page will never reconnect (a
// remote browser turned remote access off, so the server re-binds loopback
// only). A spinner here would imply "reconnecting" and never resolve.
//
// The `data-terminal` marker is load-bearing: this modal reuses the #upgrade-modal
// element, and the WS onclose handler starts health polling whenever that element
// is present. When the server re-binds and THIS remote page's socket drops, that
// handler would otherwise clobber the terminal message with "Restarting..." and
// poll a dead origin until a false "taking longer than expected" error. The marker
// tells the disconnect handler (and _startHealthPolling) to leave a terminal modal alone.
function _showRestartTerminal(message) {
    const modal = _getOrCreateUpgradeModal();
    modal.style.display = 'flex';
    modal.dataset.terminal = '1';
    const spinner = document.getElementById('upgrade-modal-spinner');
    if (spinner) spinner.style.display = 'none';
    const msg = document.getElementById('upgrade-modal-msg');
    if (msg) { msg.textContent = message; msg.className = 'upgrade-modal__msg'; }
    const dismiss = modal.querySelector('.upgrade-modal__dismiss');
    if (dismiss) dismiss.remove();
}

// Module-level so a second _startHealthPolling() call is a no-op instead of a
// second overlapping interval. The remote-access apply path and the WS
// `restart_started` handler both call this on the initiating tab; without the
// guard that tab would run two 1.5s pollers (and, the day reload becomes
// non-idempotent, double-fire).
let _healthPollInterval = null;

function _startHealthPolling() {
    // Never poll on a terminal modal: this page is not coming back.
    const existing = document.getElementById('upgrade-modal');
    if (existing && existing.dataset.terminal === '1') return;
    if (_healthPollInterval !== null) return;  // already polling
    _updateUpgradeModal('Restarting\u2026');
    const deadline = Date.now() + 30000;
    _healthPollInterval = setInterval(async () => {
        try {
            const res = await fetch('/api/health', { cache: 'no-store' });
            if (res.ok) {
                clearInterval(_healthPollInterval);
                _healthPollInterval = null;
                location.reload();
                return;  // reload in flight; don't fall through to the deadline branch
            }
        } catch (_) { /* server not up yet */ }
        if (Date.now() > deadline) {
            clearInterval(_healthPollInterval);
            _healthPollInterval = null;
            _showUpgradeError('Restart is taking longer than expected. You may need to restart jacked manually.');
        }
    }, 1500);
}

function updateVersionDisplay(versionData) {
    const el = document.getElementById('version-info');
    if (!el || !versionData) return;

    const current = versionData.current_version || versionData.current || '';
    const latest = versionData.latest_version || versionData.latest || '';
    const outdated = versionData.outdated || false;
    const ahead = versionData.ahead || false;
    const checkedAt = versionData.checked_at || '';
    const nextCheckAt = versionData.next_check_at || '';

    const tooltip = _buildTooltip(current, latest, outdated, ahead, checkedAt, nextCheckAt);

    const refresh = `<button id="version-refresh-btn" class="version-refresh" onclick="_refreshVersion()" title="Check now">\u21bb</button>`;
    const upgrade = `<button id="version-upgrade-btn" class="version-upgrade-btn" onclick="startUpgrade()" title="Install update">\u2B06</button>`;

    let badge;
    if (outdated && latest) {
        badge = `<span class="version-badge version-badge--outdated" title="${escapeHtml(tooltip)}">v${escapeHtml(current)} \u2192 v${escapeHtml(latest)} ${upgrade}${refresh}</span>`;
    } else if (ahead && latest) {
        badge = `<span class="version-badge version-badge--ahead" title="${escapeHtml(tooltip)}">v${escapeHtml(current)} dev ${refresh}</span>`;
    } else if (latest) {
        badge = `<span class="version-badge version-badge--current" title="${escapeHtml(tooltip)}">v${escapeHtml(current)} \u2713 ${refresh}</span>`;
    } else {
        badge = `<span class="version-badge version-badge--unknown" title="${escapeHtml(tooltip)}">v${escapeHtml(current)} ${refresh}</span>`;
    }

    el.innerHTML = badge;
}
