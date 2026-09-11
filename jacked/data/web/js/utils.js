/**
 * jacked web dashboard — shared utility functions
 * HTML escaping, date parsing, time formatting.
 */

// ---------------------------------------------------------------------------
// HTML escaping
// ---------------------------------------------------------------------------
function escapeHtml(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML.replace(/"/g, '&quot;');
}

function parseUTCDate(isoStr) {
    if (!isoStr) return null;
    if (/[Zz]$/.test(isoStr) || /[+-]\d{2}:\d{2}$/.test(isoStr)) return new Date(isoStr);
    return new Date(isoStr + 'Z');
}

// ---------------------------------------------------------------------------
// Date formatting
// ---------------------------------------------------------------------------
function formatResetTime(isoStr) {
    if (!isoStr) return '';
    try {
        const d = new Date(isoStr);
        const now = new Date();

        // Past reset — no active usage window
        if (d <= now) return 'no active window';

        // Same day? Show time only
        if (d.toDateString() === now.toDateString()) {
            return 'resets ' + d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
        }

        // Different day — show short date + time
        return 'resets ' + d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' + d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    } catch {
        return '';
    }
}

function timeAgo(isoStr) {
    if (!isoStr) return 'never';
    try {
        const d = new Date(isoStr);
        const seconds = Math.floor((Date.now() - d.getTime()) / 1000);
        if (seconds < 60) return 'just now';
        if (seconds < 3600) return Math.floor(seconds / 60) + 'm ago';
        if (seconds < 86400) return Math.floor(seconds / 3600) + 'h ago';
        return Math.floor(seconds / 86400) + 'd ago';
    } catch {
        return 'unknown';
    }
}

// ---------------------------------------------------------------------------
// Unix timestamp wrappers
// ---------------------------------------------------------------------------
function formatUnixResetTime(unixSeconds) {
    if (!unixSeconds) return '';
    return formatResetTime(new Date(unixSeconds * 1000).toISOString());
}

function timeAgoFromUnix(unixSeconds) {
    if (unixSeconds === null || unixSeconds === undefined) return 'never';
    return timeAgo(new Date(unixSeconds * 1000).toISOString());
}

// ---------------------------------------------------------------------------
// Token expiry time remaining (used by token pills)
// ---------------------------------------------------------------------------
const SECS_PER_DAY = 86400;
const SECS_PER_WEEK = 604800;
const TOKEN_EXPIRY_WARN_SECS = 3600;

function formatTimeRemaining(seconds) {
    if (seconds == null || isNaN(seconds) || seconds <= 0) return null;
    if (seconds < 60) return '< 1m';
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
    if (seconds < SECS_PER_DAY) {
        const h = Math.floor(seconds / 3600);
        const m = Math.floor((seconds % 3600) / 60);
        return m > 0 ? `${h}h ${m}m` : `${h}h`;
    }
    if (seconds < SECS_PER_WEEK) return `${Math.floor(seconds / SECS_PER_DAY)}d`;
    return '2w+';
}

// ---------------------------------------------------------------------------
// ISO timestamp time-ago (used by sessions and accounts)
// ---------------------------------------------------------------------------
function formatTimeAgo(isoStr) {
    try {
        const then = parseUTCDate(isoStr);
        const diffMs = Date.now() - then.getTime();
        if (diffMs < 0) return '';
        const mins = Math.floor(diffMs / 60000);
        if (mins < 1) return 'just now';
        if (mins < 60) return `${mins}m ago`;
        const hrs = Math.floor(mins / 60);
        if (hrs < 24) return `${hrs}h ago`;
        return `${Math.floor(hrs / 24)}d ago`;
    } catch {
        return '';
    }
}

// ---------------------------------------------------------------------------
// UUID generation
//
// crypto.randomUUID() only exists in a secure context (https, or http on
// localhost/127.0.0.1). With remote access on, the dashboard is served over
// plain http on a hostname or tailnet IP, where the property is undefined and
// calling it throws a TypeError. crypto.getRandomValues() is available in
// every context, so build the v4 UUID from raw bytes when randomUUID is gone.
// ---------------------------------------------------------------------------
function generateUuid() {
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
        return crypto.randomUUID();
    }
    if (typeof crypto === 'undefined' || typeof crypto.getRandomValues !== 'function') {
        throw new Error('This browser cannot generate a request id.');
    }
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 0x0f) | 0x40;  // version 4
    bytes[8] = (bytes[8] & 0x3f) | 0x80;  // RFC 4122 variant
    const hex = [];
    for (let i = 0; i < bytes.length; i++) {
        hex.push(bytes[i].toString(16).padStart(2, '0'));
    }
    const s = hex.join('');
    return `${s.slice(0, 8)}-${s.slice(8, 12)}-${s.slice(12, 16)}-${s.slice(16, 20)}-${s.slice(20)}`;
}
