/**
 * jacked web dashboard — profiles panel component
 * List, export, import, and undo security profiles.
 */

// ---------------------------------------------------------------------------
// Render
// ---------------------------------------------------------------------------

function renderProfilesPanel() {
    return `
        <div class="bg-slate-800 border border-slate-700 rounded-lg p-4 mb-4">
            <div class="flex items-center justify-between mb-4">
                <h3 class="text-sm font-medium text-slate-300">Security Profiles</h3>
                <div class="flex gap-2">
                    <button id="btn-import-profile" class="px-3 py-1.5 text-xs font-medium rounded bg-slate-700 text-slate-300 hover:bg-slate-600 hover:text-white transition-colors">
                        Import
                    </button>
                    <button id="btn-export-profile" class="px-3 py-1.5 text-xs font-medium rounded bg-blue-600 text-white hover:bg-blue-500 transition-colors">
                        Export Current
                    </button>
                </div>
            </div>
            <div id="profiles-table-container">
                <div class="flex items-center justify-center py-8">
                    <div class="spinner"></div>
                    <span class="ml-3 text-slate-400 text-sm">Loading profiles...</span>
                </div>
            </div>
        </div>
    `;
}


async function _loadProfilesTable() {
    const container = document.getElementById('profiles-table-container');
    if (!container) return;

    try {
        const data = await api.get('/api/profiles/');
        const profiles = data.profiles || [];

        if (profiles.length === 0) {
            container.innerHTML = `
                <div class="text-center py-8 text-slate-500 text-sm">
                    No saved profiles. Export your current configuration to create one.
                </div>
            `;
            return;
        }

        const rows = profiles.map(p => {
            const created = p.created_at ? _formatProfileDate(p.created_at) : '';
            return `
                <tr class="border-t border-slate-700 hover:bg-slate-750">
                    <td class="px-3 py-2 text-sm text-white font-medium">${escapeHtml(p.name)}</td>
                    <td class="px-3 py-2 text-xs text-slate-400">${escapeHtml(p.description || '-')}</td>
                    <td class="px-3 py-2 text-xs text-slate-400">${escapeHtml(p.author || '-')}</td>
                    <td class="px-3 py-2 text-xs text-slate-500 font-mono">${escapeHtml(p.jacked_version || '-')}</td>
                    <td class="px-3 py-2 text-xs text-slate-500">${escapeHtml(created)}</td>
                    <td class="px-3 py-2 text-right">
                        <button class="profile-delete-btn text-red-400 hover:text-red-300 text-xs" data-name="${escapeHtml(p.name)}">
                            Delete
                        </button>
                    </td>
                </tr>
            `;
        }).join('');

        container.innerHTML = `
            <table class="data-table w-full">
                <thead>
                    <tr>
                        <th class="text-left px-3 py-2 text-xs">Name</th>
                        <th class="text-left px-3 py-2 text-xs">Description</th>
                        <th class="text-left px-3 py-2 text-xs">Author</th>
                        <th class="text-left px-3 py-2 text-xs">Version</th>
                        <th class="text-left px-3 py-2 text-xs">Created</th>
                        <th class="w-16"></th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        `;

        // Bind delete buttons
        container.querySelectorAll('.profile-delete-btn').forEach(btn => {
            btn.addEventListener('click', () => _handleDeleteProfile(btn.dataset.name));
        });
    } catch (e) {
        container.innerHTML = `
            <div class="text-center py-8 text-red-400 text-sm">
                Failed to load profiles: ${escapeHtml(e.message)}
            </div>
        `;
    }
}


function _formatProfileDate(isoStr) {
    if (!isoStr) return '';
    try {
        const d = new Date(isoStr);
        return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
    } catch {
        return isoStr;
    }
}


// ---------------------------------------------------------------------------
// Event handlers
// ---------------------------------------------------------------------------

function bindProfilesPanelEvents() {
    _loadProfilesTable();

    const exportBtn = document.getElementById('btn-export-profile');
    if (exportBtn) {
        exportBtn.addEventListener('click', _handleExportProfile);
    }

    const importBtn = document.getElementById('btn-import-profile');
    if (importBtn) {
        importBtn.addEventListener('click', _handleImportProfile);
    }
}


async function _handleExportProfile() {
    const { value: formValues } = await Swal.fire({
        title: 'Export Profile',
        html: `
            <div class="text-left">
                <label class="block text-sm text-slate-300 mb-1">Profile Name <span class="text-red-400">*</span></label>
                <input id="swal-prof-name" class="swal2-input" placeholder="e.g. my-secure-config" maxlength="64" style="margin:0 0 0.75rem 0">
                <label class="block text-sm text-slate-300 mb-1">Description</label>
                <input id="swal-prof-desc" class="swal2-input" placeholder="Optional description" maxlength="500" style="margin:0 0 0.75rem 0">
                <label class="block text-sm text-slate-300 mb-1">Author</label>
                <input id="swal-prof-author" class="swal2-input" placeholder="Your name" maxlength="100" style="margin:0">
            </div>
        `,
        focusConfirm: false,
        showCancelButton: true,
        confirmButtonText: 'Export',
        preConfirm: () => {
            const name = document.getElementById('swal-prof-name').value.trim();
            if (!name) {
                Swal.showValidationMessage('Profile name is required');
                return false;
            }
            return {
                name,
                description: document.getElementById('swal-prof-desc').value.trim(),
                author: document.getElementById('swal-prof-author').value.trim(),
            };
        },
    });

    if (!formValues) return;

    try {
        await api.post('/api/profiles/export', formValues);
        _showProfilesToast(`Profile "${formValues.name}" exported successfully`);
        _loadProfilesTable();
    } catch (e) {
        Swal.fire('Export Failed', escapeHtml(e.message), 'error');
    }
}


async function _handleImportProfile() {
    // Open file picker
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = '.json';

    input.addEventListener('change', async () => {
        const file = input.files[0];
        if (!file) return;

        let profileData;
        try {
            const text = await file.text();
            profileData = JSON.parse(text);
        } catch {
            Swal.fire('Invalid File', 'Could not parse JSON file.', 'error');
            return;
        }

        // Validate first
        let validation;
        try {
            validation = await api.post('/api/profiles/validate', { profile: profileData });
        } catch (e) {
            Swal.fire('Validation Failed', escapeHtml(e.message), 'error');
            return;
        }

        // Show warnings if any, then confirm
        let warningHtml = '';
        if (validation.warnings && validation.warnings.length > 0) {
            const items = validation.warnings
                .map(w => `<li class="text-yellow-400 text-sm">${escapeHtml(w)}</li>`)
                .join('');
            warningHtml = `
                <div class="mt-3 mb-2 text-left">
                    <p class="text-sm text-slate-400 mb-1">Warnings:</p>
                    <ul class="list-disc pl-5">${items}</ul>
                </div>
            `;
        }

        const profileName = escapeHtml(profileData.name || file.name);
        const confirm = await Swal.fire({
            title: 'Import Profile',
            html: `
                <p class="text-sm text-slate-300">
                    Import <strong>${profileName}</strong>?
                    This will overwrite your current gatekeeper config and permission rules.
                    A backup will be created automatically.
                </p>
                ${warningHtml}
            `,
            icon: validation.warnings?.length > 0 ? 'warning' : 'question',
            showCancelButton: true,
            confirmButtonText: 'Import',
        });

        if (!confirm.isConfirmed) return;

        try {
            const result = await api.post('/api/profiles/import', { profile: profileData });
            _showProfilesUndoToast(
                `Profile "${profileName}" imported. `,
                result.backup_path
            );
            _loadProfilesTable();
        } catch (e) {
            Swal.fire('Import Failed', escapeHtml(e.message), 'error');
        }
    });

    input.click();
}


async function _handleDeleteProfile(name) {
    const confirm = await Swal.fire({
        title: 'Delete Profile',
        text: `Delete profile "${name}"? This cannot be undone.`,
        icon: 'warning',
        showCancelButton: true,
        confirmButtonText: 'Delete',
        confirmButtonColor: '#dc2626',
    });

    if (!confirm.isConfirmed) return;

    try {
        await api.delete(`/api/profiles/${encodeURIComponent(name)}`);
        _showProfilesToast(`Profile "${name}" deleted`);
        _loadProfilesTable();
    } catch (e) {
        Swal.fire('Delete Failed', escapeHtml(e.message), 'error');
    }
}


// ---------------------------------------------------------------------------
// Toasts
// ---------------------------------------------------------------------------

function _showProfilesToast(msg, isError) {
    if (typeof showLogsToast === 'function') {
        showLogsToast(msg, isError);
        return;
    }
    // Fallback toast (uses textContent to match showLogsToast contract)
    const toast = document.createElement('div');
    toast.className = `fixed bottom-2 right-2 md:bottom-6 md:right-6 left-2 md:left-auto max-w-[calc(100vw-1rem)] md:max-w-sm px-4 py-2.5 rounded-lg text-sm font-medium shadow-lg z-50 transition-opacity duration-300 ${
        isError ? 'bg-red-800 text-red-100 border border-red-600' : 'bg-green-800 text-green-100 border border-green-600'
    }`;
    toast.textContent = msg;
    document.body.appendChild(toast);
    setTimeout(() => { toast.style.opacity = '0'; setTimeout(() => toast.remove(), 300); }, 4000);
}


function _showProfilesUndoToast(message, backupPath) {
    // Remove any existing undo toast
    const existing = document.getElementById('profiles-undo-toast');
    if (existing) existing.remove();

    const toast = document.createElement('div');
    toast.id = 'profiles-undo-toast';
    toast.className = 'fixed bottom-2 right-2 md:bottom-6 md:right-6 left-2 md:left-auto max-w-[calc(100vw-1rem)] md:max-w-sm px-4 py-2.5 rounded-lg text-sm font-medium shadow-lg z-50 bg-green-800 text-green-100 border border-green-600 flex items-center gap-3';
    toast.innerHTML = `
        <span>${message}</span>
        <button id="profiles-undo-btn" class="px-2 py-0.5 text-xs font-bold rounded bg-green-600 hover:bg-green-500 text-white transition-colors">
            Undo
        </button>
    `;
    document.body.appendChild(toast);

    document.getElementById('profiles-undo-btn').addEventListener('click', async () => {
        try {
            await api.post('/api/profiles/restore');
            toast.remove();
            _showProfilesToast('Restored from backup');
            _loadProfilesTable();
        } catch (e) {
            _showProfilesToast('Restore failed: ' + (e.message || 'Unknown error'), true);
        }
    });

    // Auto-dismiss after 10 seconds
    setTimeout(() => {
        if (toast.parentNode) {
            toast.style.opacity = '0';
            setTimeout(() => toast.remove(), 300);
        }
    }, 10000);
}
