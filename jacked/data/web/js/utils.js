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
    return div.innerHTML;
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
