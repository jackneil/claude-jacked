/**
 * jacked web dashboard — Server Logs sub-tab
 * Real-time server log tail with CSS-based level coloring.
 * Depends on shared state/helpers from logs.js
 *
 * NOTE: All dynamic content uses escapeHtml() before insertion.
 * Static structural HTML uses innerHTML for template rendering (safe).
 */

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------
const _SRV_MAX_DOM_ENTRIES = 2000;

const _SRV_LEVEL_CLASS = {
    CRITICAL: 'text-red-300 font-bold',
    ERROR:    'text-red-400',
    WARNING:  'text-amber-400',
    INFO:     'text-slate-300',
    DEBUG:    'text-slate-500',
};

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let _srvPaused = false;
let _srvNewCount = 0;
let _srvActiveFilter = 'ALL';
let _srvEntryCount = 0;
let _srvLoadingInitial = false;

// ---------------------------------------------------------------------------
// Renderer (called from logs.js renderSubTab)
// ---------------------------------------------------------------------------

function renderServerLogs(container) {
    _srvPaused = false;
    _srvNewCount = 0;
    _srvEntryCount = 0;
    _srvActiveFilter = 'ALL';
    _srvLoadingInitial = false;

    // Static structural template — no dynamic data, safe for innerHTML
    container.innerHTML = `
        <div class="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 mb-4">
            <div class="flex items-center gap-3">
                <div class="text-sm text-slate-400">Live server output</div>
                <span id="srv-new-badge" class="hidden text-xs px-2 py-0.5 rounded-full bg-blue-600 text-blue-100 font-medium tabular-nums"></span>
            </div>
            <div class="flex flex-wrap items-center gap-2">
                <div id="srv-level-filters" class="flex rounded-lg overflow-hidden border border-slate-700 text-xs">
                    ${_srvFilterBtn('ALL')}${_srvFilterBtn('ERROR')}${_srvFilterBtn('WARNING')}${_srvFilterBtn('INFO')}
                </div>
                <button id="srv-pause-btn" class="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium bg-slate-900 border border-slate-700 hover:border-blue-500 text-slate-400 hover:text-blue-300 transition active:scale-[0.96]">
                    <span class="srv-pause-dot w-2 h-2 rounded-full bg-green-400 logs-live-indicator"></span>
                    <span class="srv-pause-label">Live</span>
                </button>
                <button id="srv-clear-btn" class="px-3 py-1.5 rounded-lg text-xs font-medium bg-slate-900 border border-slate-700 hover:border-blue-500 text-slate-400 hover:text-blue-300 transition active:scale-[0.96]">
                    Clear
                </button>
            </div>
        </div>
        <div id="srv-log-container" class="bg-slate-950 border border-slate-700 rounded-lg overflow-hidden">
            <div id="srv-log-output" class="font-mono text-xs leading-relaxed p-3 overflow-y-auto max-h-[70vh] min-h-[200px]">
                <div class="text-slate-600 text-center py-8">Loading server logs\u2026</div>
            </div>
        </div>
    `;

    document.getElementById('srv-pause-btn')?.addEventListener('click', _srvTogglePause);
    document.getElementById('srv-clear-btn')?.addEventListener('click', _srvClear);
    document.getElementById('srv-level-filters')?.addEventListener('click', _srvFilterClick);

    _srvLoadInitial();
}

// ---------------------------------------------------------------------------
// Initial data load
// ---------------------------------------------------------------------------

async function _srvLoadInitial() {
    const output = document.getElementById('srv-log-output');
    if (!output) return;

    _srvLoadingInitial = true;
    try {
        const data = await api.get('/api/logs/server?limit=200');
        // Re-check the DOM is still present after await
        const out = document.getElementById('srv-log-output');
        if (!out) return;

        const entries = data.entries || [];
        out.textContent = '';  // Clear loading placeholder / old content
        if (entries.length === 0) {
            const empty = document.createElement('div');
            empty.className = 'text-slate-600 text-center py-8';
            empty.textContent = 'No server logs yet';
            out.appendChild(empty);
            _srvEntryCount = 0;
            return;
        }
        const frag = document.createDocumentFragment();
        for (const entry of entries) {
            frag.appendChild(_srvCreateLine(entry));
        }
        out.appendChild(frag);
        _srvEntryCount = entries.length;
        _srvNewCount = 0;
        _srvUpdateBadge();
        _srvScrollToBottom(out);
    } catch (e) {
        const out = document.getElementById('srv-log-output');
        if (!out) return;
        out.textContent = '';
        const errDiv = document.createElement('div');
        errDiv.className = 'text-red-400 text-center py-8';
        errDiv.textContent = 'Failed to load: ' + (e.message || 'Unknown error');
        out.appendChild(errDiv);
    } finally {
        _srvLoadingInitial = false;
    }
}

// ---------------------------------------------------------------------------
// WebSocket handler (called from websocket.js event bus)
// ---------------------------------------------------------------------------

function _srvHandleWsEntries(entries) {
    if (!Array.isArray(entries) || entries.length === 0) return;
    // Skip WS entries while initial fetch is in-flight to prevent race
    if (_srvLoadingInitial) return;
    const output = document.getElementById('srv-log-output');
    if (!output) return;

    // Clear placeholder if present
    if (_srvEntryCount === 0) {
        const placeholder = output.querySelector('.text-slate-600');
        if (placeholder) output.textContent = '';
    }

    requestAnimationFrame(() => {
        const countBefore = output.children.length;
        const frag = document.createDocumentFragment();
        for (const entry of entries) {
            frag.appendChild(_srvCreateLine(entry));
        }
        output.appendChild(frag);

        // DOM trimming
        while (output.children.length > _SRV_MAX_DOM_ENTRIES) {
            output.removeChild(output.firstChild);
        }
        _srvEntryCount = output.children.length;

        if (_srvPaused) {
            // Count actual new entries (after trim), not raw batch size
            _srvNewCount += Math.max(0, output.children.length - countBefore);
            _srvUpdateBadge();
        } else {
            _srvScrollToBottom(output);
        }
    });
}

// ---------------------------------------------------------------------------
// Disconnect indicator
// ---------------------------------------------------------------------------

function _srvInsertDisconnectMarker() {
    const output = document.getElementById('srv-log-output');
    if (!output) return;
    const marker = document.createElement('div');
    marker.className = 'text-center text-slate-600 text-[10px] py-1 border-t border-b border-slate-800 my-1';
    marker.textContent = '\u2014 connection interrupted \u2014';
    output.appendChild(marker);
}

// ---------------------------------------------------------------------------
// Line rendering (all dynamic content escaped via escapeHtml / textContent)
// ---------------------------------------------------------------------------

function _srvCreateLine(entry) {
    const div = document.createElement('div');
    const levelCls = _SRV_LEVEL_CLASS[entry.level] || 'text-slate-400';
    const filterCls = _srvShouldHide(entry.level) ? ' hidden' : '';
    div.className = 'srv-log-line py-0.5 hover:bg-slate-900/50 break-all' + filterCls;
    div.dataset.level = entry.level || '';

    // Build via DOM nodes — no innerHTML with dynamic data
    const tsSpan = document.createElement('span');
    tsSpan.className = 'text-slate-600';
    tsSpan.textContent = _srvFormatTs(entry.ts) + ' ';

    const loggerSpan = document.createElement('span');
    loggerSpan.className = 'text-slate-600';
    loggerSpan.textContent = (entry.logger ? entry.logger.split('.').pop() : '') + ' ';

    const msgSpan = document.createElement('span');
    msgSpan.className = levelCls;
    msgSpan.textContent = entry.msg || '';

    div.appendChild(tsSpan);
    div.appendChild(loggerSpan);
    div.appendChild(msgSpan);

    return div;
}

function _srvFormatTs(isoStr) {
    if (!isoStr) return '';
    try {
        const d = new Date(isoStr);
        return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    } catch { return ''; }
}

// ---------------------------------------------------------------------------
// Pause / Resume
// ---------------------------------------------------------------------------

function _srvTogglePause() {
    _srvPaused = !_srvPaused;
    const dot = document.querySelector('.srv-pause-dot');
    const label = document.querySelector('.srv-pause-label');
    if (dot) {
        dot.className = _srvPaused
            ? 'srv-pause-dot w-2 h-2 rounded-full bg-amber-400'
            : 'srv-pause-dot w-2 h-2 rounded-full bg-green-400 logs-live-indicator';
    }
    if (label) label.textContent = _srvPaused ? 'Paused' : 'Live';

    if (!_srvPaused) {
        _srvNewCount = 0;
        _srvUpdateBadge();
        const output = document.getElementById('srv-log-output');
        if (output) _srvScrollToBottom(output);
    }
}

function _srvUpdateBadge() {
    const badge = document.getElementById('srv-new-badge');
    if (!badge) return;
    if (_srvNewCount > 0) {
        badge.textContent = _srvNewCount + ' new';
        badge.classList.remove('hidden');
    } else {
        badge.classList.add('hidden');
    }
}

// ---------------------------------------------------------------------------
// Level filter
// ---------------------------------------------------------------------------

function _srvFilterBtn(level) {
    const isActive = _srvActiveFilter === level;
    const cls = isActive
        ? 'bg-slate-700 text-white'
        : 'bg-slate-900 text-slate-500 hover:text-slate-300';
    return '<button class="srv-filter-btn px-2.5 py-1 ' + cls + ' transition-colors" data-level="' + level + '">' + level + '</button>';
}

function _srvFilterClick(e) {
    const btn = e.target.closest('.srv-filter-btn');
    if (!btn) return;
    _srvActiveFilter = btn.dataset.level;

    // Update button styles
    document.querySelectorAll('.srv-filter-btn').forEach(b => {
        if (b.dataset.level === _srvActiveFilter) {
            b.className = 'srv-filter-btn px-2.5 py-1 bg-slate-700 text-white transition-colors';
        } else {
            b.className = 'srv-filter-btn px-2.5 py-1 bg-slate-900 text-slate-500 hover:text-slate-300 transition-colors';
        }
    });

    // Toggle visibility via CSS class (batched to avoid main-thread jank)
    requestAnimationFrame(() => {
        const output = document.getElementById('srv-log-output');
        if (!output) return;
        output.querySelectorAll('.srv-log-line').forEach(line => {
            if (_srvShouldHide(line.dataset.level)) {
                line.classList.add('hidden');
            } else {
                line.classList.remove('hidden');
            }
        });
    });
}

function _srvShouldHide(level) {
    if (_srvActiveFilter === 'ALL') return false;
    const order = { CRITICAL: 4, ERROR: 3, WARNING: 2, INFO: 1, DEBUG: 0 };
    const minOrd = order[_srvActiveFilter] || 0;
    return (order[level] || 0) < minOrd;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function _srvClear() {
    const output = document.getElementById('srv-log-output');
    if (output) {
        output.textContent = '';
        const msg = document.createElement('div');
        msg.className = 'text-slate-600 text-center py-8';
        msg.textContent = 'Display cleared';
        output.appendChild(msg);
        _srvEntryCount = 0;
        _srvNewCount = 0;
        _srvUpdateBadge();
        // Reset pause state inline (avoid side-effects from _srvTogglePause)
        if (_srvPaused) {
            _srvPaused = false;
            const dot = document.querySelector('.srv-pause-dot');
            const label = document.querySelector('.srv-pause-label');
            if (dot) dot.className = 'srv-pause-dot w-2 h-2 rounded-full bg-green-400 logs-live-indicator';
            if (label) label.textContent = 'Live';
        }
    }
}

function _srvScrollToBottom(el) {
    el.scrollTop = el.scrollHeight;
}

// ---------------------------------------------------------------------------
// Refresh (called from logs.js refreshCurrentLogsSubTab)
// ---------------------------------------------------------------------------

function refreshServerLogs() {
    // Re-fetch the buffer on WS reconnect to fill any gap from the
    // disconnected period. Safe to call at any time.
    if (document.getElementById('srv-log-output')) {
        _srvLoadInitial();
    }
}
