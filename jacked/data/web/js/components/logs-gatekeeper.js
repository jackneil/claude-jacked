/** jacked web dashboard — Gatekeeper sub-tab (depends on logs.js) */

let logsMethodFilter = 'ALL';
let _methodOptions = [];  // cached method values from API

// --- Pattern extraction ---
const _ENV_PREFIX_RE = /^(\w+=\S+\s+)+/;

const _KNOWN_COMMAND_PREFIXES = new Set([
    // 2-word prefixes
    'git push', 'git commit', 'git pull', 'git checkout', 'git merge', 'git rebase',
    'git clone', 'git fetch', 'git stash', 'git diff', 'git log', 'git add',
    'npm install', 'npm run', 'npm test', 'npm start', 'npm exec',
    'npx create', 'yarn add', 'yarn run',
    'pip install', 'pip uninstall',
    'docker exec', 'docker run', 'docker build', 'docker compose',
    'cargo build', 'cargo test', 'cargo run',
    'go build', 'go test', 'go run', 'go get',
    'uv run', 'uv pip',
    // 3-word prefixes
    'python -m pytest', 'python -m pip', 'python -m venv',
    'docker compose up', 'docker compose down', 'docker compose build', 'docker compose logs',
    'npx create react', 'npx create next',
    'uv run python', 'uv pip install',
]);

// Commands whose broad allow rules bypass LLM safety evaluation
const _SECURITY_SENSITIVE_BASES = new Set([
    'curl', 'wget',
    'pip install', 'npm install', 'npx', 'bunx',
    'cargo install', 'gem install', 'go install',
    'pipx install', 'yarn add', 'pnpx',
    'uv pip install', 'uv tool install',
]);

function _isSensitive(prefix) {
    for (const s of _SECURITY_SENSITIVE_BASES) {
        if (prefix === s || prefix.startsWith(s + ' ') || s.startsWith(prefix + ' ')) return true;
    }
    return false;
}

function tokenizeForSelector(command, method) {
    if (!command) return null;

    // PATH_SAFETY with [ToolName] /path format → path segment selector.
    // If the command is a raw Bash string (e.g., grep ... /path/.env), fall
    // through to the standard Bash tokenizer below.
    if (method === 'PATH_SAFETY') {
        const pathMatch = command.match(/^\[?(\w+)\]?\s+(\/.+)/);
        if (pathMatch) {
            const toolName = pathMatch[1];
            const pattern = pathMatch[2].trim();
            const segments = pattern.slice(1).split('/').filter(Boolean); // drop leading /
            if (!segments.length) return { type: 'path', pattern, toolName, segments: [], recommendedIndex: 0 };
            const recommendedIndex = Math.max(0, segments.length - 2);
            return { type: 'path', pattern, toolName, segments, recommendedIndex };
        }
        // Not in [Tool] /path format — fall through to Bash tokenizer
    }

    const stripped = command.replace(_ENV_PREFIX_RE, '');
    const tokens = stripped.split(/\s+/).filter(Boolean);
    if (!tokens.length) return null;

    // Find recommended boundary index (0-based, inclusive)
    // Check 3-word then 2-word dictionary match
    let recommendedIndex = tokens.length - 1; // default: exact match (safe)
    for (const len of [3, 2]) {
        if (tokens.length >= len) {
            const candidate = tokens.slice(0, len).join(' ');
            if (_KNOWN_COMMAND_PREFIXES.has(candidate)) {
                recommendedIndex = len - 1;
                break;
            }
        }
    }

    return { type: 'tokens', tokens, stripped, recommendedIndex };
}

function _patternForBoundary(tokens, boundaryIndex) {
    if (boundaryIndex >= tokens.length - 1) {
        return `Bash(${tokens.join(' ')})`;
    }
    const prefix = tokens.slice(0, boundaryIndex + 1).join(' ');
    return `Bash(${prefix}:*)`;
}

function _descriptionForBoundary(tokens, boundaryIndex) {
    if (boundaryIndex >= tokens.length - 1) {
        return 'Allow only this exact command';
    }
    const prefix = tokens.slice(0, boundaryIndex + 1).join(' ');
    return `Allow ${prefix} with any arguments`;
}

function _pathPatternForBoundary(toolName, segments, boundaryIndex) {
    // Escape chars that are special in the pattern syntax (:, *, (, ))
    const safeSegments = segments.slice(0, boundaryIndex + 1).map(
        s => s.replace(/[:\*\(\)]/g, '_')
    );
    const path = '/' + safeSegments.join('/');
    if (boundaryIndex >= segments.length - 1) {
        return `${toolName}(${path})`;
    }
    return `${toolName}(${path}:*)`;
}

function _pathDescriptionForBoundary(toolName, segments, boundaryIndex) {
    const path = '/' + segments.slice(0, boundaryIndex + 1).join('/');
    if (boundaryIndex >= segments.length - 1) {
        return `Allow ${toolName} only for this exact path`;
    }
    return `Allow ${toolName} for all files under ${path}/`;
}

// --- File-tool names (project-scope warning) ---
const _FILE_TOOL_NAMES = new Set(['Read', 'Edit', 'Write', 'Grep', 'Glob', 'NotebookEdit']);

function _isFileToolPattern(pattern) {
    const match = pattern.match(/^(\w+)\(/);
    return match && _FILE_TOOL_NAMES.has(match[1]);
}

// --- Always Allow modal ---
function showAlwaysAllowModal({ tokenData, repoPath }) {
    const existing = document.getElementById('always-allow-modal-overlay');
    if (existing) existing.remove();

    const isPath = tokenData.type === 'path';
    const repoName = repoPath ? repoPath.replace(/\\/g, '/').split('/').filter(Boolean).pop() : '';

    const overlay = document.createElement('div');
    overlay.id = 'always-allow-modal-overlay';
    overlay.className = 'fixed inset-0 bg-black/60 flex items-center justify-center z-50';

    const modal = document.createElement('div');
    modal.className = 'bg-slate-800 border border-slate-600 rounded-xl shadow-2xl w-full max-w-lg mx-4 p-6';

    // Title
    const title = document.createElement('h3');
    title.className = 'text-lg font-semibold text-white mb-4';
    title.textContent = isPath ? 'Add Path Rule' : 'Always Allow Rule';
    modal.appendChild(title);

    // Track selected pattern for submission
    let selectedPattern = '';
    let useCustom = false;

    if (isPath) {
        // Path safety: token pill selector for path segments
        const { segments, toolName, recommendedIndex: pathRecIdx } = tokenData;
        // Clamp initial boundary: at least 3 segments (index 2) for prefix patterns,
        // or fall back to exact match (last segment) if path is too short.
        const minBoundary = segments.length <= 2 ? segments.length - 1 : 2;
        let pathBoundary = Math.max(pathRecIdx, minBoundary);

        const pathPatternRow = document.createElement('div');
        pathPatternRow.className = 'mb-1';
        const pathPatternCode = document.createElement('code');
        pathPatternCode.className = 'text-xs font-mono text-blue-300';

        const pathDescRow = document.createElement('div');
        pathDescRow.className = 'text-xs text-slate-400 mb-3';

        // Instruction label
        const pathInstrLabel = document.createElement('div');
        pathInstrLabel.className = 'text-sm text-slate-300 mb-2';
        pathInstrLabel.textContent = 'Click to set allow boundary:';
        modal.appendChild(pathInstrLabel);

        // Token pill container for path segments
        const pathTokenContainer = document.createElement('div');
        pathTokenContainer.className = 'mb-4 bg-slate-900/50 rounded-lg px-3 py-3';

        const pathTokenRow = document.createElement('div');
        pathTokenRow.className = 'flex flex-wrap items-center gap-0.5';

        function renderPathTokens() {
            while (pathTokenRow.firstChild) pathTokenRow.removeChild(pathTokenRow.firstChild);
            // Leading /
            const slash0 = document.createElement('span');
            slash0.className = 'text-slate-500 font-mono text-sm select-none';
            slash0.textContent = '/';
            pathTokenRow.appendChild(slash0);

            // Minimum boundary index for 3 directory segments (API rejects < 3).
            // Last segment (exact file match) is always allowed regardless.
            const minPrefixBoundary = 2;

            segments.forEach((seg, i) => {
                const pill = document.createElement('span');
                const isExactMatch = (i === segments.length - 1);
                const isTooShallow = (i < minPrefixBoundary && !isExactMatch);
                pill.className = 'rounded px-2 py-0.5 text-sm font-mono transition-colors select-none';
                if (isTooShallow) {
                    pill.className += ' text-slate-600 cursor-not-allowed';
                } else if (i <= pathBoundary) {
                    pill.className += ' cursor-pointer text-white bg-slate-700 hover:bg-slate-600';
                } else {
                    pill.className += ' cursor-pointer text-slate-500 hover:bg-slate-700/40';
                }
                pill.textContent = seg;
                if (!isTooShallow) {
                    pill.addEventListener('click', () => {
                        pathBoundary = i;
                        selectedPattern = _pathPatternForBoundary(toolName, segments, pathBoundary);
                        renderPathTokens();
                        updatePathDisplay();
                    });
                }
                pathTokenRow.appendChild(pill);

                // Wildcard indicator after boundary (if not exact match)
                if (i === pathBoundary && pathBoundary < segments.length - 1) {
                    const star = document.createElement('span');
                    star.className = 'text-blue-400 font-bold text-sm select-none mx-0.5';
                    star.textContent = '/*';
                    pathTokenRow.appendChild(star);
                }

                // Slash separator between segments (after non-last segments beyond boundary)
                if (i < segments.length - 1 && i !== pathBoundary) {
                    const sep = document.createElement('span');
                    sep.className = 'text-slate-500 font-mono text-sm select-none';
                    sep.textContent = '/';
                    pathTokenRow.appendChild(sep);
                }
            });

            // Recommended badge
            const existingBadge = pathTokenContainer.querySelector('.rec-badge');
            if (existingBadge) existingBadge.remove();
            const badge = document.createElement('div');
            badge.className = 'rec-badge text-[10px] text-blue-300 mt-1.5';
            // Show "Recommended" arrow if at the recommended boundary, or if the
            // recommended index points to a disabled (too-shallow) pill.
            const recIsDisabled = pathRecIdx < minPrefixBoundary && pathRecIdx !== segments.length - 1;
            if (pathBoundary === pathRecIdx || recIsDisabled) {
                badge.textContent = '\u2191 Recommended';
            } else {
                badge.textContent = 'Recommended: click ';
                const recSpan = document.createElement('span');
                recSpan.className = 'font-mono text-blue-400';
                recSpan.textContent = segments[pathRecIdx];
                badge.appendChild(recSpan);
            }
            pathTokenContainer.appendChild(badge);
        }

        function updatePathDisplay() {
            pathPatternCode.textContent = selectedPattern;
            pathDescRow.textContent = _pathDescriptionForBoundary(toolName, segments, pathBoundary);
        }

        pathTokenContainer.appendChild(pathTokenRow);
        modal.appendChild(pathTokenContainer);

        pathPatternRow.appendChild(pathPatternCode);
        modal.appendChild(pathPatternRow);
        modal.appendChild(pathDescRow);

        // Set initial state
        selectedPattern = _pathPatternForBoundary(toolName, segments, pathBoundary);
        renderPathTokens();
        updatePathDisplay();
    } else {
        const { tokens, recommendedIndex } = tokenData;
        let boundaryIndex = recommendedIndex;

        // Pattern display elements (created early, updated by renderTokens)
        const patternRow = document.createElement('div');
        patternRow.className = 'mb-1';
        const patternCode = document.createElement('code');
        patternCode.className = 'text-xs font-mono text-blue-300';

        const descRow = document.createElement('div');
        descRow.className = 'text-xs text-slate-400 mb-3';

        const warningRow = document.createElement('div');
        warningRow.className = 'text-[11px] text-amber-400 mb-3 hidden';

        // Instruction label
        const instrLabel = document.createElement('div');
        instrLabel.className = 'text-sm text-slate-300 mb-2';
        instrLabel.textContent = 'Click to set allow boundary:';
        modal.appendChild(instrLabel);

        // Token pill container
        const tokenContainer = document.createElement('div');
        tokenContainer.className = 'mb-4 bg-slate-900/50 rounded-lg px-3 py-3';

        const tokenRow = document.createElement('div');
        tokenRow.className = 'flex flex-wrap items-center gap-1.5';

        function renderTokens() {
            tokenRow.innerHTML = '';
            tokens.forEach((tok, i) => {
                const pill = document.createElement('span');
                pill.className = 'cursor-pointer rounded px-2 py-0.5 text-sm font-mono transition-colors select-none';
                if (i <= boundaryIndex) {
                    pill.className += ' text-white bg-slate-700 hover:bg-slate-600';
                } else {
                    pill.className += ' text-slate-500 hover:bg-slate-700/40';
                }
                pill.textContent = tok;
                pill.addEventListener('click', () => {
                    boundaryIndex = i;
                    useCustom = false;
                    selectedPattern = _patternForBoundary(tokens, boundaryIndex);
                    renderTokens();
                    updateDisplay();
                });
                tokenRow.appendChild(pill);

                // Insert wildcard indicator after boundary (if not exact match)
                if (i === boundaryIndex && boundaryIndex < tokens.length - 1) {
                    const star = document.createElement('span');
                    star.className = 'text-blue-400 font-bold text-sm select-none';
                    star.textContent = '*';
                    tokenRow.appendChild(star);
                }
            });

            // Recommended badge
            const existingBadge = tokenContainer.querySelector('.rec-badge');
            if (existingBadge) existingBadge.remove();
            const badge = document.createElement('div');
            badge.className = 'rec-badge text-[10px] text-blue-300 mt-1.5';
            // Position the badge text to indicate which token is recommended
            const recTok = tokens[recommendedIndex];
            if (boundaryIndex === recommendedIndex) {
                badge.innerHTML = '\u2191 Recommended';
            } else {
                badge.innerHTML = `Recommended: click <span class="font-mono text-blue-400">${recTok}</span>`;
            }
            tokenContainer.appendChild(badge);
        }

        function updateDisplay() {
            if (useCustom) return;
            patternCode.textContent = selectedPattern;
            descRow.textContent = _descriptionForBoundary(tokens, boundaryIndex);
            // Security warning
            const prefix = tokens.slice(0, boundaryIndex + 1).join(' ');
            if (boundaryIndex < tokens.length - 1 && _isSensitive(prefix)) {
                warningRow.textContent = '\u26A0 Bypasses LLM safety checks for this command category';
                warningRow.classList.remove('hidden');
            } else {
                warningRow.classList.add('hidden');
            }
        }

        tokenContainer.appendChild(tokenRow);
        modal.appendChild(tokenContainer);

        // Pattern + description display
        patternRow.appendChild(patternCode);
        modal.appendChild(patternRow);
        modal.appendChild(descRow);
        modal.appendChild(warningRow);

        // Custom pattern toggle
        const customToggle = document.createElement('div');
        customToggle.className = 'mb-4';
        const customLink = document.createElement('button');
        customLink.className = 'text-xs text-slate-500 hover:text-slate-300 transition-colors';
        customLink.textContent = 'Custom pattern\u2026';
        const customInput = document.createElement('input');
        customInput.type = 'text';
        customInput.className = 'w-full bg-slate-900 border border-slate-600 rounded-lg px-3 py-2 text-sm text-slate-200 font-mono focus:outline-none focus:border-blue-500 mt-2 hidden';
        const customWarn = document.createElement('div');
        customWarn.className = 'text-[11px] text-amber-400 mt-1 hidden';

        customLink.addEventListener('click', () => {
            useCustom = !useCustom;
            if (useCustom) {
                customInput.classList.remove('hidden');
                customInput.value = selectedPattern;
                customInput.focus();
                customLink.textContent = 'Use token selector';
            } else {
                customInput.classList.add('hidden');
                customWarn.classList.add('hidden');
                customLink.textContent = 'Custom pattern\u2026';
                selectedPattern = _patternForBoundary(tokens, boundaryIndex);
                updateDisplay();
            }
        });

        customInput.addEventListener('input', () => {
            selectedPattern = customInput.value.trim();
            patternCode.textContent = selectedPattern;
            descRow.textContent = 'Custom pattern';
            const m = selectedPattern.match(/^Bash\((.+?)(?::?\*?)?\)$/);
            if (m && _isSensitive(m[1])) {
                customWarn.textContent = '\u26A0 This pattern bypasses LLM safety checks';
                customWarn.classList.remove('hidden');
            } else {
                customWarn.classList.add('hidden');
            }
        });

        customToggle.appendChild(customLink);
        customToggle.appendChild(customInput);
        customToggle.appendChild(customWarn);
        modal.appendChild(customToggle);

        // Set initial state
        selectedPattern = _patternForBoundary(tokens, boundaryIndex);
        renderTokens();
        updateDisplay();
    }

    // Scope selection
    const scopeWarning = document.createElement('div');
    scopeWarning.className = 'text-xs text-yellow-400 mb-3 hidden';

    {
        const scopeLabel = document.createElement('div');
        scopeLabel.className = 'text-sm text-slate-300 mb-2';
        scopeLabel.textContent = 'Scope:';
        modal.appendChild(scopeLabel);

        const scopeRow = document.createElement('div');
        scopeRow.className = 'flex gap-3 mb-3';

        const scopeGlobal = document.createElement('label');
        scopeGlobal.className = 'flex items-center gap-2 text-sm text-slate-300 cursor-pointer';
        const radioGlobal = document.createElement('input');
        radioGlobal.type = 'radio';
        radioGlobal.name = 'aa-scope';
        radioGlobal.value = 'global';
        radioGlobal.checked = true;
        radioGlobal.className = 'accent-blue-500';
        scopeGlobal.appendChild(radioGlobal);
        scopeGlobal.appendChild(document.createTextNode('Global'));
        scopeRow.appendChild(scopeGlobal);

        if (repoPath) {
            const scopeProject = document.createElement('label');
            const radioProject = document.createElement('input');
            radioProject.type = 'radio';
            radioProject.name = 'aa-scope';
            radioProject.value = 'project';
            radioProject.className = 'accent-blue-500';
            if (isPath) {
                // Path rules only work at global scope — disable project radio
                radioProject.disabled = true;
                scopeProject.className = 'flex items-center gap-2 text-sm text-slate-500 cursor-not-allowed';
            } else {
                scopeProject.className = 'flex items-center gap-2 text-sm text-slate-300 cursor-pointer';
            }
            scopeProject.appendChild(radioProject);
            scopeProject.appendChild(document.createTextNode(`Project: ${repoName}`));
            scopeRow.appendChild(scopeProject);
        }

        modal.appendChild(scopeRow);

        if (isPath) {
            // Static note for path rules
            const pathScopeNote = document.createElement('div');
            pathScopeNote.className = 'text-xs text-slate-500 mb-3';
            pathScopeNote.textContent = 'Path rules are always global \u2014 they apply across all projects';
            modal.appendChild(pathScopeNote);
        }

        modal.appendChild(scopeWarning);

        const updateScopeWarning = () => {
            const scope = modal.querySelector('input[name="aa-scope"]:checked')?.value || 'global';
            if (scope === 'project' && _isFileToolPattern(selectedPattern)) {
                scopeWarning.textContent = 'Note: File-tool rules (Read, Edit, Write, Grep, Glob, NotebookEdit) only apply at global scope.';
                scopeWarning.classList.remove('hidden');
            } else {
                scopeWarning.classList.add('hidden');
            }
        };
        modal.addEventListener('change', updateScopeWarning);
    }

    // Buttons
    const btnRow = document.createElement('div');
    btnRow.className = 'flex justify-end gap-3 mt-4';

    const cancelBtn = document.createElement('button');
    cancelBtn.className = 'px-4 py-2 rounded-lg text-sm font-medium bg-slate-700 hover:bg-slate-600 text-slate-300 transition-colors';
    cancelBtn.textContent = 'Cancel';
    cancelBtn.addEventListener('click', () => overlay.remove());

    const addBtn = document.createElement('button');
    addBtn.className = 'px-4 py-2 rounded-lg text-sm font-medium bg-blue-600 hover:bg-blue-500 text-white transition-colors';
    addBtn.textContent = 'Add Rule';
    addBtn.addEventListener('click', async () => {
        const val = selectedPattern;
        if (!val) return;

        addBtn.disabled = true;
        addBtn.textContent = 'Adding...';

        try {
            const scope = modal.querySelector('input[name="aa-scope"]:checked')?.value || 'global';
            const payload = { pattern: val, list_name: 'allow', scope };
            if (scope === 'project' && repoPath) {
                payload.repo_path = repoPath;
            }
            await api.post('/api/claude-settings/permissions/rule', payload);
            overlay.remove();
            showLogsToast(`Added rule: ${val}`);
        } catch (e) {
            showLogsToast('Failed: ' + e.message, true);
            addBtn.disabled = false;
            addBtn.textContent = 'Add Rule';
        }
    });

    btnRow.appendChild(cancelBtn);
    btnRow.appendChild(addBtn);
    modal.appendChild(btnRow);

    // Close on overlay click
    overlay.addEventListener('click', (e) => {
        if (e.target === overlay) overlay.remove();
    });

    // Escape key
    overlay.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') overlay.remove();
    });

    overlay.appendChild(modal);
    document.body.appendChild(overlay);
    overlay.setAttribute('tabindex', '-1');
    overlay.focus();
}

// --- Gatekeeper sub-tab renderer ---
function renderGatekeeperSubTab(container) {
    container.innerHTML = `
        <div class="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 mb-4">
            <div class="text-sm text-slate-400">Security gatekeeper decisions</div>
            <div class="flex flex-wrap items-center gap-2">
                <input id="logs-search" type="text" placeholder="Search commands..."
                    class="bg-slate-900 border border-slate-700 rounded-lg px-3 py-1.5 text-sm text-slate-200 placeholder-slate-500 focus:outline-none focus:border-blue-500 w-full sm:w-48"
                    value="${escapeHtml(logsSearch)}">
                <select id="logs-filter" class="bg-slate-900 border border-slate-700 rounded-lg px-3 py-1.5 text-sm text-slate-200 focus:outline-none focus:border-blue-500">
                    <option value="ALL" ${logsFilter === 'ALL' ? 'selected' : ''}>All Decisions</option>
                    <option value="ALLOW" ${logsFilter === 'ALLOW' ? 'selected' : ''}>Allowed</option>
                    <option value="ASK_USER" ${logsFilter === 'ASK_USER' ? 'selected' : ''}>Asked User</option>
                    <option value="DEFER_TO_CC" ${logsFilter === 'DEFER_TO_CC' ? 'selected' : ''}>Deferred</option>
                </select>
                <select id="logs-method-filter" class="bg-slate-900 border border-slate-700 rounded-lg px-3 py-1.5 text-sm text-slate-200 focus:outline-none focus:border-blue-500">
                    <option value="ALL">All Methods</option>
                    ${_methodOptions.map(m => `<option value="${escapeHtml(m)}" ${logsMethodFilter === m ? 'selected' : ''}>${escapeHtml(m)}</option>`).join('')}
                </select>
                <select id="logs-repo-filter" class="bg-slate-900 border border-slate-700 rounded-lg px-3 py-1.5 text-sm text-slate-200 focus:outline-none focus:border-blue-500">
                    <option value="ALL">All Repos</option>
                </select>
                ${renderPauseButton()}
                <button id="logs-export-btn" title="Export as JSON"
                    class="p-1.5 rounded-lg bg-slate-900 border border-slate-700 hover:border-blue-500 text-slate-400 hover:text-blue-300 transition-colors">
                    <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/>
                    </svg>
                </button>
                <button id="logs-purge-btn" title="Purge old logs"
                    class="p-1.5 rounded-lg bg-slate-900 border border-slate-700 hover:border-red-500 text-slate-400 hover:text-red-300 transition-colors">
                    <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/>
                    </svg>
                </button>
            </div>
        </div>

        <div id="logs-purge-bar" class="hidden mb-4"></div>

        <div id="logs-sessions" class="mb-4">
            <div class="flex items-center gap-2 text-xs text-slate-500">
                <div class="spinner" style="width:14px;height:14px"></div> Loading sessions...
            </div>
        </div>

        <div id="logs-content">
            <div class="flex items-center justify-center py-12">
                <div class="spinner"></div>
                <span class="ml-3 text-slate-400 text-sm">Loading logs...</span>
            </div>
        </div>
    `;

    bindGatekeeperLogsEvents();
    _loadMethodOptions();
    loadSessions().then(() => loadLogsData());
}

// --- Event binding ---
function bindGatekeeperLogsEvents() {
    // Parse drill-down params from hash (e.g. #logs?decision=ALLOW&from=analytics)
    const hashParts = window.location.hash.split('?');
    const hashParams = new URLSearchParams(hashParts[1] || '');

    if (hashParams.has('decision')) {
        logsFilter = hashParams.get('decision');
    }
    if (hashParams.has('method')) {
        logsMethodFilter = hashParams.get('method');
    }
    if (hashParams.has('session_id')) {
        logsActiveSession = hashParams.get('session_id');
    }

    // Show "Back to Dashboard" link when coming from analytics
    if (hashParams.get('from') === 'analytics') {
        const backLink = document.createElement('a');
        backLink.href = '#analytics';
        backLink.className = 'inline-flex items-center gap-1 text-xs text-blue-400 hover:text-blue-300 mb-3 transition-colors';
        backLink.innerHTML = '<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 19l-7-7 7-7"/></svg> Back to Dashboard';
        backLink.addEventListener('click', () => {
            // Clean hash params so they don't persist
            window.location.hash = 'analytics';
        });
        const logsContent = document.getElementById('logs-subtab-content');
        if (logsContent) {
            logsContent.insertBefore(backLink, logsContent.firstChild);
        }
    }

    const filterEl = document.getElementById('logs-filter');
    if (filterEl) {
        filterEl.value = logsFilter;
        filterEl.addEventListener('change', () => {
            logsFilter = filterEl.value;
            gkPage = 0;
            loadLogsData();
        });
    }

    const methodFilterEl = document.getElementById('logs-method-filter');
    if (methodFilterEl) {
        methodFilterEl.addEventListener('change', () => {
            logsMethodFilter = methodFilterEl.value;
            gkPage = 0;
            loadLogsData();
        });
    }

    const searchEl = document.getElementById('logs-search');
    if (searchEl) {
        let debounce = null;
        searchEl.addEventListener('input', () => {
            clearTimeout(debounce);
            debounce = setTimeout(() => {
                logsSearch = searchEl.value;
                gkPage = 0;
                loadLogsData();
            }, 300);
        });
    }

    const repoFilterEl = document.getElementById('logs-repo-filter');
    if (repoFilterEl) {
        repoFilterEl.addEventListener('change', () => {
            logsActiveRepo = repoFilterEl.value;
            logsActiveSession = 'ALL';
            gkPage = 0;
            renderFilteredSessions();
            loadLogsData();
        });
    }

    const exportBtn = document.getElementById('logs-export-btn');
    if (exportBtn) exportBtn.addEventListener('click', doExport);

    const purgeBtn = document.getElementById('logs-purge-btn');
    if (purgeBtn) purgeBtn.addEventListener('click', showPurgeBar);
}

// --- Session cards ---
function getRepoName(repoPath) {
    if (!repoPath) return '';
    return repoPath.replace(/\\/g, '/').split('/').filter(Boolean).pop() || repoPath;
}
function getUniqueRepos(sessions) {
    const seen = new Map();
    for (const s of sessions) {
        if (s.repo_path) {
            const key = s.repo_path.toLowerCase();
            if (!seen.has(key)) seen.set(key, s.repo_path);
        }
    }
    return [...seen.values()].sort((a, b) =>
        getRepoName(a).localeCompare(getRepoName(b))
    );
}
function isSessionActive(lastSeen) {
    if (!lastSeen) return false;
    try {
        const d = parseUTCDate(lastSeen);
        return (Date.now() - d.getTime()) < 5 * 60 * 1000;
    } catch { return false; }
}

function renderSessionCards(sessions, activeId) {
    const totalDecisions = sessions.reduce((sum, s) => sum + (s.total || 0), 0);

    const allCard = `
        <button class="session-card flex-shrink-0 rounded-lg px-3 py-2.5 text-left transition-all cursor-pointer min-w-[80px]
            ${activeId === 'ALL'
                ? 'bg-blue-900/40 border-2 border-blue-500 ring-1 ring-blue-500/30'
                : 'bg-slate-800 border border-slate-700 hover:border-slate-500'}"
            data-session="ALL">
            <div class="text-xs font-semibold text-slate-300 uppercase tracking-wider">All</div>
            <div class="text-lg font-bold text-white mt-0.5">${totalDecisions}</div>
            <div class="text-xs text-slate-500">decisions</div>
        </button>
    `;

    const sessionCards = sessions.map(s => {
        const sid = s.session_id || '';
        const shortId = sid.substring(0, 8);
        const repo = getRepoName(s.repo_path);
        const active = isSessionActive(s.last_seen);
        const isSelected = activeId === sid;
        const firstTime = formatLogTimestamp(s.first_seen);
        const lastTime = formatLogTimestamp(s.last_seen);

        return `
            <button class="session-card flex-shrink-0 rounded-lg px-3 py-2.5 text-left transition-all cursor-pointer min-w-[180px] max-w-[240px]
                ${isSelected
                    ? 'bg-blue-900/40 border-2 border-blue-500 ring-1 ring-blue-500/30'
                    : 'bg-slate-800 border border-slate-700 hover:border-slate-500'}"
                data-session="${escapeHtml(sid)}">
                <div class="flex items-center gap-2">
                    <span class="font-mono text-xs font-semibold ${isSelected ? 'text-blue-300' : 'text-slate-300'}">${shortId}</span>
                    ${active ? '<span class="w-2 h-2 rounded-full bg-green-400 animate-pulse"></span>' : ''}
                </div>
                <div class="text-xs text-slate-400 mt-1 truncate" title="${escapeHtml(s.repo_path || '')}">${escapeHtml(repo)}</div>
                <div class="flex items-center gap-2 mt-1.5">
                    <span class="text-xs font-medium text-white">${s.total || 0}</span>
                    ${(s.allowed || 0) > 0 ? `<span class="inline-block px-1.5 py-0 rounded text-[10px] font-medium bg-green-800/60 text-green-300">${s.allowed}A</span>` : ''}
                    ${(s.asked || 0) > 0 ? `<span class="inline-block px-1.5 py-0 rounded text-[10px] font-medium bg-yellow-800/60 text-yellow-300">${s.asked}U</span>` : ''}
                </div>
                <div class="text-[10px] text-slate-500 mt-1">${firstTime} → ${lastTime}</div>
            </button>
        `;
    }).join('');

    return `
        <div class="flex gap-2 overflow-x-auto pb-2 scrollbar-thin" style="scrollbar-width: thin;">
            ${allCard}
            ${sessionCards}
        </div>
    `;
}

async function _loadMethodOptions() {
    try {
        const data = await api.get('/api/logs/gatekeeper/methods');
        _methodOptions = data.methods || [];
        const select = document.getElementById('logs-method-filter');
        if (select) {
            const options = ['<option value="ALL">All Methods</option>']
                .concat(_methodOptions.map(m =>
                    `<option value="${escapeHtml(m)}" ${logsMethodFilter === m ? 'selected' : ''}>${escapeHtml(m)}</option>`
                ));
            select.innerHTML = options.join('');
        }
    } catch (e) {
        console.error('Failed to load method options:', e);
    }
}

async function loadSessions() {
    try {
        logsSessions = await api.get('/api/logs/sessions');
    } catch (e) {
        console.error('Failed to load sessions:', e);
        logsSessions = [];
    }
    renderFilteredSessions();
}

function renderFilteredSessions() {
    const container = document.getElementById('logs-sessions');
    if (!container) return;

    if (logsSessions.length === 0) {
        container.innerHTML = '';
        return;
    }

    if (logsActiveRepo !== 'ALL') {
        const repoExists = logsSessions.some(s =>
            (s.repo_path || '').toLowerCase() === logsActiveRepo
        );
        if (!repoExists) logsActiveRepo = 'ALL';
    }

    let filtered = logsSessions;
    if (logsActiveRepo !== 'ALL') {
        filtered = logsSessions.filter(s =>
            (s.repo_path || '').toLowerCase() === logsActiveRepo
        );
    }

    container.innerHTML = renderSessionCards(filtered, logsActiveSession);
    bindSessionCardClicks(container);
    updateRepoDropdown();
}

function updateRepoDropdown() {
    const select = document.getElementById('logs-repo-filter');
    if (!select) return;
    const uniqueRepos = getUniqueRepos(logsSessions);
    const options = ['<option value="ALL"' + (logsActiveRepo === 'ALL' ? ' selected' : '') + '>All Repos</option>']
        .concat(uniqueRepos.map(r =>
            `<option value="${escapeHtml(r.toLowerCase())}"${logsActiveRepo === r.toLowerCase() ? ' selected' : ''}>${escapeHtml(getRepoName(r))}</option>`
        ));
    select.innerHTML = options.join('');
}

function bindSessionCardClicks(container) {
    container.querySelectorAll('.session-card').forEach(card => {
        card.addEventListener('click', () => {
            logsActiveSession = card.dataset.session;
            gkPage = 0;
            renderFilteredSessions();
            loadLogsData();
        });
    });
}

// --- Decision colors & formatting ---
function getDecisionColors(decision, method) {
    if (decision === 'ALLOW') {
        if (method === 'PERMS' || method === 'LOCAL') {
            return { bg: 'bg-green-900/20', badge: 'bg-green-700 text-green-100' };
        }
        return { bg: 'bg-emerald-900/20', badge: 'bg-emerald-700 text-emerald-100' };
    }
    if (method === 'DENY_PATTERN') {
        return { bg: 'bg-red-900/20', badge: 'bg-red-700 text-red-100' };
    }
    if (decision === 'DEFER_TO_CC') {
        return { bg: 'bg-slate-900/20', badge: 'bg-slate-600 text-slate-200' };
    }
    return { bg: 'bg-yellow-900/20', badge: 'bg-yellow-700 text-yellow-100' };
}
function truncateCommand(cmd, maxLen) {
    if (!cmd) return '';
    return cmd.length <= maxLen ? cmd : cmd.substring(0, maxLen) + '...';
}

// --- Row HTML builder (reused by full render and incremental prepend) ---
// Note: all dynamic values are escaped via escapeHtml() before interpolation.
function _parseTrajectory(raw) {
    if (raw == null) return null;
    try {
        return typeof raw === 'string' ? JSON.parse(raw) : raw;
    } catch (e) {
        console.warn('Failed to parse trajectory:', e);
        return null;
    }
}

function _renderTrajectory(traj) {
    if (!Array.isArray(traj) || !traj.length) return '';
    const TIER_LABELS = {
        deny_pattern: 'Deny', category: 'Category',
        path_safety: 'Path', path_safety_floor: 'Path Floor',
        perms: 'Perms', local: 'Local', llm: 'LLM'
    };
    const pills = traj.map(step => {
        const label = TIER_LABELS[step.tier] || escapeHtml(step.tier || '');
        const detail = step.detail ? ` \u00b7 ${escapeHtml(String(step.detail).substring(0, 40))}` : '';
        const msVal = Number(step.ms);
        const ms = !isNaN(msVal) ? ` ${msVal < 1 ? '<1' : Math.round(msVal)}ms` : '';
        if (step.result === 'pass') {
            return `<span class="inline-flex items-center gap-1 px-2 py-0.5 rounded border border-transparent text-[10px] bg-slate-700/50 text-slate-400"><span class="text-green-500">\u2713</span>${label}<span class="text-slate-500">${ms}</span></span>`;
        }
        const isAllow = step.result === 'allow';
        const color = isAllow
            ? 'bg-green-900/40 text-green-300 border border-green-700/50'
            : 'bg-amber-900/40 text-amber-300 border border-amber-700/50';
        return `<span class="inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] ${color}">${label}${detail}<span class="opacity-60">${ms}</span></span>`;
    });
    return `<div>
        <div class="text-[10px] uppercase tracking-wider text-slate-500 mb-1">Decision Path</div>
        <div class="flex items-center gap-1 flex-wrap">${pills.join('<span class="text-slate-600 text-[10px]">\u2192</span>')}</div>
    </div>`;
}

function buildRowHtml(r, showRepo) {
    const colors = getDecisionColors(r.decision, r.method);
    const cmd = escapeHtml(truncateCommand(r.command, 100));
    const fullCmd = escapeHtml(r.command || '');
    const method = escapeHtml(r.method || '-');
    const reason = r.reason ? escapeHtml(r.reason) : '';
    const elapsed = formatDuration(r.elapsed_ms);
    const ts = formatLogTimestamp(r.timestamp);
    const repo = getRepoName(r.repo_path);
    const fullRepo = escapeHtml(r.repo_path || '');
    const session = r.session_id ? r.session_id.substring(0, 8) : '';
    const fullSession = escapeHtml(r.session_id || '');
    const colSpan = showRepo ? 6 : 5;

    return `
        <tr class="${colors.bg} hover:bg-slate-700/50 transition-colors cursor-pointer log-row" data-id="${r.id}">
            <td class="px-3 py-2 text-xs text-slate-400 whitespace-nowrap font-mono">${ts}</td>
            <td class="px-3 py-2">
                <span class="inline-block px-2 py-0.5 rounded text-xs font-medium ${colors.badge}">${escapeHtml(r.decision)}</span>
            </td>
            <td class="px-3 py-2 text-xs text-slate-300 whitespace-nowrap">${method}</td>
            <td class="px-3 py-2 max-w-[200px] md:max-w-md">
                <div class="text-sm font-mono text-slate-200 truncate">${cmd}</div>
                ${reason ? `<div class="text-xs text-slate-400 italic truncate mt-0.5">${reason}</div>` : ''}
            </td>
            <td class="px-3 py-2 text-xs text-slate-400 whitespace-nowrap text-right">${elapsed}</td>
            ${showRepo
                ? `<td class="px-3 py-2 text-xs text-slate-500 whitespace-nowrap font-mono">${escapeHtml(repo || session)}</td>`
                : ''}
        </tr>
        <tr class="log-detail hidden" data-id="${r.id}">
            <td colspan="${colSpan}" class="px-4 py-3 ${colors.bg} border-t border-slate-700/30">
                <div class="space-y-2">
                    <div>
                        <div class="text-[10px] uppercase tracking-wider text-slate-500 mb-1">Full Command</div>
                        <pre class="text-xs font-mono text-slate-200 whitespace-pre-wrap break-all bg-slate-900/50 rounded px-3 py-2 max-h-40 overflow-y-auto">${fullCmd}</pre>
                    </div>
                    ${reason ? `
                    <div>
                        <div class="text-[10px] uppercase tracking-wider text-slate-500 mb-1">Reason</div>
                        <div class="text-xs text-slate-300 italic">${reason}</div>
                    </div>` : ''}
                    ${_renderTrajectory(_parseTrajectory(r.trajectory))}
                    <div class="flex items-center gap-6 text-xs text-slate-400">
                        <div><span class="text-slate-500">Session:</span> <span class="font-mono">${fullSession}</span></div>
                        <div><span class="text-slate-500">Repo:</span> <span class="font-mono">${fullRepo}</span></div>
                        <div><span class="text-slate-500">Elapsed:</span> ${elapsed}</div>
                        ${r.decision === 'ASK_USER' && r.method !== 'PATH_SAFETY_FLOOR' ? `
                        <button class="always-allow-btn ml-auto px-3 py-1 rounded-lg text-xs font-medium bg-blue-700 hover:bg-blue-600 text-white transition-colors"
                            data-command="${fullCmd}"
                            data-method="${method}"
                            data-repo="${fullRepo}">
                            ${r.method === 'PATH_SAFETY' ? 'Add Allowed Path' : 'Always Allow'}
                        </button>` : ''}
                    </div>
                </div>
            </td>
        </tr>
    `;
}

// --- Bind row events (click-to-expand + always-allow) on a container element ---
function _bindRowEvents(root) {
    root.querySelectorAll('.log-row').forEach(row => {
        row.addEventListener('click', () => {
            const id = row.dataset.id;
            const detail = row.parentElement
                ? row.parentElement.querySelector(`.log-detail[data-id="${id}"]`)
                : null;
            if (detail) detail.classList.toggle('hidden');
        });
    });

    root.querySelectorAll('.always-allow-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            const command = btn.dataset.command;
            const method = btn.dataset.method;
            const repo = btn.dataset.repo;
            const tokenData = tokenizeForSelector(command, method);
            if (!tokenData) {
                showLogsToast('Could not extract pattern from this command', true);
                return;
            }
            showAlwaysAllowModal({ tokenData, repoPath: repo });
        });
    });
}

// --- Gatekeeper decision table (server-side paginated) ---
let _gkFetchGen = 0;

async function loadLogsData(incremental = false) {
    const container = document.getElementById('logs-content');
    if (!container) return;

    // Page-0 guard: incremental only works on the first page
    if (incremental && gkPage !== 0) {
        return loadLogsData(false);
    }

    const myGen = ++_gkFetchGen;

    window.jackedState.logsInFlight = true;
    try {
        let url = `/api/logs/gatekeeper?limit=${gkPageSize}&offset=${gkPage * gkPageSize}`;
        if (logsFilter !== 'ALL') url += `&decision=${logsFilter}`;
        if (logsMethodFilter !== 'ALL') url += `&method=${encodeURIComponent(logsMethodFilter)}`;
        if (logsActiveSession !== 'ALL') url += `&session_id=${encodeURIComponent(logsActiveSession)}`;
        if (logsSearch) url += `&command_search=${encodeURIComponent(logsSearch)}`;
        if (logsActiveRepo !== 'ALL') url += `&repo_path=${encodeURIComponent(logsActiveRepo)}`;

        const data = await api.get(url);

        // Discard stale fetch (user changed filters/page while we were waiting)
        if (myGen !== _gkFetchGen) return;

        const rows = data.rows || [];
        gkTotal = data.total || 0;

        // Auto-clamp page if total dropped (e.g., after purge)
        const maxPage = Math.max(0, Math.ceil(gkTotal / gkPageSize) - 1);
        if (gkPage > maxPage) {
            gkPage = maxPage;
            window.jackedState.logsInFlight = false;
            return loadLogsData(false);
        }

        // --- Incremental path: prepend only new rows ---
        if (incremental) {
            const tbody = container.querySelector('tbody');
            if (tbody && rows.length > 0) {
                const existingIds = new Set();
                tbody.querySelectorAll('.log-row').forEach(el => existingIds.add(el.dataset.id));

                const showRepo = logsActiveSession === 'ALL';
                const newRows = rows.filter(r => !existingIds.has(String(r.id)));

                if (newRows.length > 0) {
                    // Build new rows in a temporary tbody to parse HTML into nodes
                    const tempTable = document.createElement('table');
                    const tempBody = document.createElement('tbody');
                    tempTable.appendChild(tempBody);
                    tempBody.insertAdjacentHTML('beforeend', newRows.map(r => buildRowHtml(r, showRepo)).join(''));

                    // Bind events on new rows before inserting into the live DOM
                    _bindRowEvents(tempBody);

                    // Prepend new rows to the live tbody
                    const fragment = document.createDocumentFragment();
                    while (tempBody.firstChild) {
                        fragment.appendChild(tempBody.firstChild);
                    }
                    tbody.insertBefore(fragment, tbody.firstChild);
                }

                // Re-render pagination (outside tbody, no user state to preserve)
                const paginationEl = container.querySelector('.pagination-controls');
                if (paginationEl) {
                    paginationEl.outerHTML = renderPagination('gk', gkPage, gkPageSize, gkTotal);
                }
                return;
            }
            // tbody not found or empty rows — fall through to full render
        }

        // --- Full render path ---
        if (rows.length === 0) {
            container.innerHTML = `
                <div class="bg-slate-900 border border-slate-700 rounded-lg px-6 py-12 text-center">
                    <svg class="w-10 h-10 text-slate-600 mx-auto mb-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2"/>
                    </svg>
                    <div class="text-slate-400 text-sm">No gatekeeper decisions${logsActiveSession !== 'ALL' ? ' for this session' : ''}</div>
                    <div class="text-slate-500 text-xs mt-1">Decisions appear here as the gatekeeper evaluates commands</div>
                </div>
                ${gkTotal > 0 ? renderPagination('gk', gkPage, gkPageSize, gkTotal) : ''}
            `;
            return;
        }

        const showRepo = logsActiveSession === 'ALL';
        const rowsHtml = rows.map(r => buildRowHtml(r, showRepo)).join('');

        container.innerHTML = `
            <div class="bg-slate-900 border border-slate-700 rounded-lg overflow-hidden overflow-x-auto">
                <table class="w-full">
                    <thead>
                        <tr class="border-b border-slate-700">
                            <th class="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Time</th>
                            <th class="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Decision</th>
                            <th class="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Method</th>
                            <th class="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Command</th>
                            <th class="px-3 py-2 text-right text-xs font-medium text-slate-400 uppercase">Elapsed</th>
                            ${showRepo ? '<th class="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Repo</th>' : ''}
                        </tr>
                    </thead>
                    <tbody class="divide-y divide-slate-700/50">
                        ${rowsHtml}
                    </tbody>
                </table>
            </div>
            ${renderPagination('gk', gkPage, gkPageSize, gkTotal)}
        `;

        _bindRowEvents(container);
    } catch (e) {
        container.innerHTML = `
            <div class="bg-red-900/30 border border-red-700 rounded-lg px-4 py-3 text-sm text-red-200">
                Failed to load logs: ${escapeHtml(e.message)}
            </div>
        `;
    } finally {
        window.jackedState.logsInFlight = false;
    }
}

function gkChangePageSize(val) { gkPageSize = parseInt(val, 10) || 50; gkPage = 0; loadLogsData(); }
function gkPrevPage() { if (gkPage > 0) { gkPage--; loadLogsData(); } }
function gkNextPage() { if ((gkPage + 1) * gkPageSize < gkTotal) { gkPage++; loadLogsData(); } }

// --- Purge & Export ---
function showPurgeBar() {
    const bar = document.getElementById('logs-purge-bar');
    if (!bar) return;

    const sessionBtn = logsActiveSession !== 'ALL'
        ? `<button id="purge-session-btn" class="px-3 py-1 rounded text-xs font-medium bg-red-700 hover:bg-red-600 text-white transition-colors">
               Purge this session (${logsActiveSession.substring(0, 8)})
           </button>`
        : '';

    bar.innerHTML = `
        <div class="bg-red-900/30 border border-red-700 rounded-lg px-4 py-3">
            <div class="flex items-center gap-3 flex-wrap">
                <svg class="w-4 h-4 text-red-400 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.5L13.732 4c-.77-.833-1.964-.833-2.732 0L4.082 16.5c-.77.833.192 2.5 1.732 2.5z"/>
                </svg>
                <span class="text-sm text-red-200">Purge logs older than</span>
                <select id="purge-age" class="bg-slate-800 border border-red-700 rounded px-2 py-1 text-xs text-slate-200 focus:outline-none">
                    <option value="1">1 day</option>
                    <option value="7" selected>7 days</option>
                    <option value="30">30 days</option>
                    <option value="all">All</option>
                </select>
                <button id="purge-confirm-btn" class="px-3 py-1 rounded text-xs font-medium bg-red-700 hover:bg-red-600 text-white transition-colors">
                    Purge
                </button>
                ${sessionBtn}
                <button id="purge-cancel-btn" class="px-3 py-1 rounded text-xs font-medium bg-slate-700 hover:bg-slate-600 text-slate-300 transition-colors">
                    Cancel
                </button>
            </div>
            <div class="text-[11px] text-red-300/70 mt-1.5">Purged logs are also removed from Analytics.</div>
        </div>
    `;
    bar.classList.remove('hidden');

    document.getElementById('purge-confirm-btn').addEventListener('click', () => {
        const ageVal = document.getElementById('purge-age').value;
        if (ageVal === 'all') {
            doPurge(null, null);
        } else {
            doPurge(parseInt(ageVal, 10), null);
        }
    });

    document.getElementById('purge-cancel-btn').addEventListener('click', hidePurgeBar);

    const sessionPurgeBtn = document.getElementById('purge-session-btn');
    if (sessionPurgeBtn) {
        sessionPurgeBtn.addEventListener('click', () => doPurge(null, logsActiveSession));
    }
}

function hidePurgeBar() {
    const bar = document.getElementById('logs-purge-bar');
    if (bar) {
        bar.classList.add('hidden');
        bar.innerHTML = '';
    }
}

async function doPurge(olderThanDays, sessionId) {
    try {
        let url = '/api/logs/gatekeeper';
        const params = [];
        if (olderThanDays != null) params.push(`older_than_days=${olderThanDays}`);
        if (sessionId) params.push(`session_id=${encodeURIComponent(sessionId)}`);
        if (params.length) url += '?' + params.join('&');

        const res = await api.delete(url);
        const count = res.purged || 0;
        hidePurgeBar();
        showLogsToast(`${count} decision${count !== 1 ? 's' : ''} purged`);
        gkPage = 0;
        await loadSessions();
        await loadLogsData();
    } catch (e) {
        showLogsToast('Purge failed: ' + e.message, true);
    }
}

async function doExport() {
    try {
        let url = '/api/logs/gatekeeper/export?';
        const params = [];
        if (logsActiveSession !== 'ALL') params.push(`session_id=${encodeURIComponent(logsActiveSession)}`);
        if (logsFilter !== 'ALL') params.push(`decision=${logsFilter}`);
        url += params.join('&');

        const resp = await fetch(url);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const blob = await resp.blob();
        const disposition = resp.headers.get('Content-Disposition') || '';
        const match = disposition.match(/filename="(.+?)"/);
        const filename = match ? match[1] : 'gatekeeper-logs.json';

        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = filename;
        a.click();
        URL.revokeObjectURL(a.href);

        showLogsToast('Exported ' + filename);
    } catch (e) {
        showLogsToast('Export failed: ' + e.message, true);
    }
}
