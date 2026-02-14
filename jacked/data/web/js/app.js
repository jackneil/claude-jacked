/**
 * jacked web dashboard — main application module
 * Router, global state, API client, polling
 */

// ---------------------------------------------------------------------------
// Global state
// ---------------------------------------------------------------------------
window.jackedState = {
    accounts: [],
    installations: [],
    settings: {},
    version: { current: '', latest: '', outdated: false },
    activeRoute: 'accounts',
    activeCredentialAccountId: null,
    activeSessions: {},
    polling: null,
    flowPolling: null,
    logsPaused: false,
    logsInFlight: false,
    // Session display preferences (persisted via localStorage)
    sessionStalenessMs: parseInt(localStorage.getItem('jacked_session_staleness') || '3600000'),
    sessionGroupByRepo: localStorage.getItem('jacked_session_group_repo') === '1',
    sessionShowSubagents: localStorage.getItem('jacked_session_show_subagents') !== '0',
    expandedRepoGroups: new Set(),
    sessionLookupResult: null,
};

// ---------------------------------------------------------------------------
// API Client
// ---------------------------------------------------------------------------
const api = {
    async _request(method, path, body) {
        const opts = {
            method,
            headers: { 'Content-Type': 'application/json' },
        };
        if (body !== undefined) {
            opts.body = JSON.stringify(body);
        }
        try {
            const res = await fetch(path, opts);
            if (!res.ok) {
                const err = await res.json().catch(() => ({ error: { message: res.statusText } }));
                throw new ApiError(err.error?.message || res.statusText, res.status, err.error?.code);
            }
            // 204 No Content
            if (res.status === 204) return null;
            return await res.json();
        } catch (e) {
            if (e instanceof ApiError) throw e;
            throw new ApiError(e.message || 'Network error', 0, 'NETWORK_ERROR');
        }
    },

    get(path) { return this._request('GET', path); },
    post(path, body) { return this._request('POST', path, body); },
    patch(path, body) { return this._request('PATCH', path, body); },
    put(path, body) { return this._request('PUT', path, body); },
    delete(path) { return this._request('DELETE', path); },
};

class ApiError extends Error {
    constructor(message, status, code) {
        super(message);
        this.name = 'ApiError';
        this.status = status;
        this.code = code;
    }
}

// ---------------------------------------------------------------------------
// Router
// ---------------------------------------------------------------------------
const ROUTES = ['accounts', 'installations', 'settings', 'logs', 'analytics'];

function getRoute() {
    const hash = window.location.hash.replace('#', '') || 'accounts';
    return ROUTES.includes(hash) ? hash : 'accounts';
}

function navigateTo(route) {
    window.location.hash = route;
}

function updateNavHighlight(route) {
    document.querySelectorAll('.nav-link').forEach(link => {
        const linkRoute = link.getAttribute('data-route');
        if (linkRoute === route) {
            link.classList.add('active');
        } else {
            link.classList.remove('active');
        }
    });
}

async function renderRoute(route) {
    const content = document.getElementById('content');
    window.jackedState.activeRoute = route;
    updateNavHighlight(route);

    // Save active tab to localStorage
    localStorage.setItem('jacked_active_tab', route);

    switch (route) {
        case 'accounts':
            if (typeof renderAccounts === 'function') {
                content.innerHTML = renderAccounts(window.jackedState.accounts);
                if (typeof bindAccountEvents === 'function') bindAccountEvents();
                // Auto-validate stale accounts on mount
                autoValidateStaleAccounts();
            }
            break;
        case 'installations':
            if (typeof renderInstallations === 'function') {
                content.innerHTML = renderInstallations();
                if (typeof bindInstallationEvents === 'function') bindInstallationEvents();
            }
            break;
        case 'settings':
            if (typeof renderSettings === 'function') {
                content.innerHTML = renderSettings(window.jackedState.settings);
                if (typeof bindSettingsEvents === 'function') bindSettingsEvents();
            }
            break;
        case 'logs':
            if (typeof renderLogs === 'function') {
                content.innerHTML = renderLogs();
                if (typeof bindLogsEvents === 'function') bindLogsEvents();
            }
            break;
        case 'analytics':
            if (typeof renderAnalytics === 'function') {
                content.innerHTML = renderAnalytics();
                if (typeof bindAnalyticsEvents === 'function') bindAnalyticsEvents();
            }
            break;
        default:
            content.innerHTML = '<div class="text-slate-500 p-8">Unknown route</div>';
    }
}

// ---------------------------------------------------------------------------
// Data loading
// ---------------------------------------------------------------------------
async function loadAccounts() {
    try {
        const data = await api.get('/api/auth/accounts');
        window.jackedState.accounts = data.accounts || data || [];
    } catch (e) {
        console.error('Failed to load accounts:', e);
        showToast('Failed to load accounts', 'error');
    }
}

async function loadInstallations() {
    try {
        const data = await api.get('/api/installations');
        window.jackedState.installations = data.installations || data || [];
    } catch (e) {
        console.error('Failed to load installations:', e);
    }
}

async function loadSettings() {
    try {
        const data = await api.get('/api/settings');
        window.jackedState.settings = data.settings || data || {};
    } catch (e) {
        console.error('Failed to load settings:', e);
    }
}

async function loadVersion() {
    try {
        const data = await api.get('/api/version');
        window.jackedState.version = data;
        if (typeof updateVersionDisplay === 'function') {
            updateVersionDisplay(data);
        }
    } catch (e) {
        console.error('Failed to load version:', e);
    }
}

async function loadActiveSessions() {
    try {
        const mins = Math.round(window.jackedState.sessionStalenessMs / 60000) || 60;
        const data = await api.get(`/api/auth/active-sessions?staleness=${mins}`);
        window.jackedState.activeSessions = data.sessions || {};
    } catch (e) {
        console.error('Failed to load active sessions:', e);
    }
}

async function loadAllData() {
    await Promise.all([
        loadAccounts(),
        loadActiveSessions(),
        loadSettings(),
        loadVersion(),
        typeof loadActiveCredential === 'function' ? loadActiveCredential() : Promise.resolve(),
    ]);
}

async function refreshAndRender() {
    await loadAllData();
    renderRoute(getRoute());
}

// ---------------------------------------------------------------------------
// Centralized accounts re-render (preserves UI expansion state)
// ---------------------------------------------------------------------------
function rerenderAccountsView() {
    if (window.jackedState.activeRoute !== 'accounts') return;
    if (typeof renderAccounts !== 'function') return;

    const content = document.getElementById('content');
    // Save expanded account details
    const expandedDetails = new Set();
    document.querySelectorAll('.account-details:not(.hidden)').forEach(el => {
        expandedDetails.add(el.dataset.detailsId);
    });
    // Save expanded repo groups
    const savedRepoGroups = new Set(window.jackedState.expandedRepoGroups);
    // Save session lookup input value
    const savedLookupValue = document.getElementById('inp-session-lookup')?.value || '';

    content.innerHTML = renderAccounts(window.jackedState.accounts);
    if (typeof bindAccountEvents === 'function') bindAccountEvents();

    // Restore expanded details
    expandedDetails.forEach(id => {
        const details = document.querySelector(`.account-details[data-details-id="${id}"]`);
        const btn = document.querySelector(`.btn-toggle-details[data-id="${id}"]`);
        if (details) {
            details.classList.remove('hidden');
            const arrow = btn?.querySelector('.details-arrow');
            if (arrow) arrow.innerHTML = '&#9650;';
            if (btn) btn.childNodes[0].textContent = 'Hide details ';
        }
    });
    // Restore session lookup input value
    const restoredInput = document.getElementById('inp-session-lookup');
    if (restoredInput && savedLookupValue) restoredInput.value = savedLookupValue;
    // Restore repo group expansion
    window.jackedState.expandedRepoGroups = savedRepoGroups;
    savedRepoGroups.forEach(key => {
        const sessionsEl = [...document.querySelectorAll('.repo-group-sessions')].find(el => el.dataset.groupKey === key);
        const chevron = [...document.querySelectorAll('.repo-group-chevron')].find(el => el.dataset.groupKey === key);
        if (sessionsEl) sessionsEl.classList.remove('hidden');
        if (chevron) chevron.classList.add('expanded');
    });
}

// ---------------------------------------------------------------------------
// Auto-validation for stale accounts
// ---------------------------------------------------------------------------
async function autoValidateStaleAccounts() {
    const now = Math.floor(Date.now() / 1000);
    const oneHourAgo = now - 3600;
    const maxValidations = 5;

    const stale = window.jackedState.accounts.filter(acct => {
        if (!acct.is_active) return false;
        if (acct.validation_status === 'checking') return false;
        if (acct.validation_status === 'invalid') return false;
        if (acct.last_validated_at && acct.last_validated_at > oneHourAgo) return false;
        return true;
    }).slice(0, maxValidations);

    for (const acct of stale) {
        try {
            await api.post(`/api/auth/accounts/${acct.id}/validate`);
        } catch {
            // swallow — validation errors are recorded server-side
        }
        if (stale.indexOf(acct) < stale.length - 1) {
            await new Promise(r => setTimeout(r, 1000));
        }
    }

    if (stale.length > 0) {
        await loadAccounts();
        rerenderAccountsView();
    }
}

// ---------------------------------------------------------------------------
// Polling (30s default, 120s when WebSocket connected)
// ---------------------------------------------------------------------------
function _currentPollInterval() {
    return (typeof jackedWS !== 'undefined' && jackedWS.isConnected()) ? 120000 : 30000;
}

function startPolling() {
    stopPolling();
    window.jackedState.polling = setInterval(async () => {
        if (typeof _accountActionInFlight !== 'undefined' && _accountActionInFlight) return;
        await loadAccounts();
        await loadActiveSessions();
        rerenderAccountsView();
    }, _currentPollInterval());
}

function stopPolling() {
    if (window.jackedState.polling) {
        clearInterval(window.jackedState.polling);
        window.jackedState.polling = null;
    }
}

// ---------------------------------------------------------------------------
// Toast notifications
// ---------------------------------------------------------------------------
function showToast(message, type = 'info', duration = 4000) {
    const container = document.getElementById('toast-container');
    if (!container) return;

    const colors = {
        info: 'bg-blue-900 border-blue-700 text-blue-200',
        success: 'bg-green-900 border-green-700 text-green-200',
        error: 'bg-red-900 border-red-700 text-red-200',
        warning: 'bg-yellow-900 border-yellow-700 text-yellow-200',
    };

    const toast = document.createElement('div');
    toast.className = `toast border rounded-lg px-4 py-3 text-sm shadow-lg max-w-full md:max-w-sm ${colors[type] || colors.info}`;
    toast.textContent = message;
    container.appendChild(toast);

    setTimeout(() => {
        toast.classList.add('removing');
        setTimeout(() => toast.remove(), 200);
    }, duration);
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
document.addEventListener('DOMContentLoaded', async () => {
    // Restore last active tab
    const savedTab = localStorage.getItem('jacked_active_tab');
    if (savedTab && ROUTES.includes(savedTab) && !window.location.hash) {
        window.location.hash = savedTab;
    }

    // Load data and render
    await loadAllData();
    renderRoute(getRoute());

    // Start account polling
    startPolling();

    // Connect WebSocket event bus (handlers defined in websocket.js)
    if (typeof jackedWS !== 'undefined') {
        jackedWS.connect();
    }

    // Unsaved changes guard — warn on page close/refresh
    window.addEventListener('beforeunload', (e) => {
        if (window._settingsDirty) {
            e.preventDefault();
        }
    });

    // Hash change listener — guard against leaving settings with unsaved changes
    let _suppressHashChange = false;
    window.addEventListener('hashchange', async () => {
        if (_suppressHashChange) { _suppressHashChange = false; return; }
        if (window._settingsDirty) {
            const result = await Swal.fire({
                title: 'Unsaved Changes',
                text: 'You have unsaved settings changes. Leave without saving?',
                icon: 'warning',
                showCancelButton: true,
                confirmButtonText: 'Leave',
                cancelButtonText: 'Stay',
                focusCancel: true,
            });
            if (!result.isConfirmed) {
                _suppressHashChange = true;
                window.location.hash = 'settings';
                return;
            }
            window._settingsDirty = false;
        }
        renderRoute(getRoute());
    });

    // Global refresh button
    const refreshBtn = document.getElementById('btn-refresh-all');
    if (refreshBtn) {
        refreshBtn.addEventListener('click', async () => {
            refreshBtn.disabled = true;
            refreshBtn.innerHTML = '<div class="spinner"></div> Refreshing...';
            await refreshAndRender();
            refreshBtn.disabled = false;
            refreshBtn.innerHTML = '<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg> Refresh All';
            showToast('All data refreshed', 'success');
        });
    }

    // Mobile sidebar toggle
    const sidebar = document.getElementById('sidebar');
    const sidebarToggle = document.getElementById('sidebar-toggle');
    const sidebarBackdrop = document.getElementById('sidebar-backdrop');

    function openSidebar() {
        sidebar.classList.remove('hidden');
        sidebar.classList.add('flex', 'flex-col');
        sidebarBackdrop.classList.remove('hidden');
        document.body.classList.add('overflow-hidden');
    }

    function closeSidebar() {
        sidebar.classList.add('hidden');
        sidebar.classList.remove('flex', 'flex-col');
        sidebarBackdrop.classList.add('hidden');
        document.body.classList.remove('overflow-hidden');
    }

    if (sidebarToggle) {
        sidebarToggle.addEventListener('click', () => {
            if (sidebar.classList.contains('hidden')) {
                openSidebar();
            } else {
                closeSidebar();
            }
        });
    }

    if (sidebarBackdrop) {
        sidebarBackdrop.addEventListener('click', closeSidebar);
    }

    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && !sidebarBackdrop.classList.contains('hidden')) {
            closeSidebar();
        }
    });

    // Close sidebar on nav link click (mobile)
    document.querySelectorAll('.nav-link').forEach(link => {
        link.addEventListener('click', () => {
            if (window.innerWidth < 768) {
                closeSidebar();
            }
        });
    });

    // Reset sidebar state when crossing md breakpoint
    window.matchMedia('(min-width: 768px)').addEventListener('change', (e) => {
        if (e.matches) {
            // Crossed to desktop — reset mobile sidebar state
            sidebar.classList.remove('flex', 'flex-col');
            sidebar.classList.remove('hidden');
            sidebarBackdrop.classList.add('hidden');
            document.body.classList.remove('overflow-hidden');
        } else {
            // Crossed to mobile — hide sidebar
            sidebar.classList.add('hidden');
            sidebar.classList.remove('flex', 'flex-col');
            sidebarBackdrop.classList.add('hidden');
            document.body.classList.remove('overflow-hidden');
        }
    });
});
