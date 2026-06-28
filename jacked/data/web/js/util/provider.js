/**
 * jacked — provider visual identity (Claude vs Codex).
 *
 * Single source of truth for the per-account provider mark so the dashboard
 * cards, the menu-bar panel rows, and any future surface render the same
 * brand-colored logo + label and can never disagree (same pattern as the
 * shared renderUsageBar). Brand colors: Claude = Anthropic terracotta,
 * Codex = OpenAI green. Unknown/missing provider falls back to Claude.
 */

function providerMeta(provider) {
    const p = String(provider || 'claude').toLowerCase();
    if (p === 'codex') {
        return {
            key: 'codex',
            label: 'Codex',
            color: '#10a37f',
            // OpenAI-style knot: three rotated ellipses.
            svg:
                '<svg viewBox="0 0 16 16" width="100%" height="100%" fill="none" ' +
                'stroke="currentColor" stroke-width="1.3" aria-hidden="true">' +
                '<ellipse cx="8" cy="8" rx="2.8" ry="6.2"/>' +
                '<ellipse cx="8" cy="8" rx="2.8" ry="6.2" transform="rotate(60 8 8)"/>' +
                '<ellipse cx="8" cy="8" rx="2.8" ry="6.2" transform="rotate(120 8 8)"/>' +
                '</svg>',
        };
    }
    return {
        key: 'claude',
        label: 'Claude',
        color: '#d97757',
        // Anthropic-style sunburst: tapered rays from center.
        svg:
            '<svg viewBox="0 0 16 16" width="100%" height="100%" stroke="currentColor" ' +
            'stroke-width="1.5" stroke-linecap="round" aria-hidden="true">' +
            [0, 45, 90, 135]
                .map((a) => `<line x1="8" y1="2.4" x2="8" y2="13.6" transform="rotate(${a} 8 8)"/>`)
                .join('') +
            '</svg>',
    };
}

/** Compact glyph (logo only, brand-colored) — for tight surfaces (the panel). */
function providerGlyph(provider) {
    const m = providerMeta(provider);
    return (
        `<span class="provider-glyph provider-${m.key}" title="${m.label} account" ` +
        `aria-label="${m.label} account" style="color:${m.color}">${m.svg}</span>`
    );
}

/** Full badge (logo + label chip) — for the roomier dashboard cards. */
function providerBadge(provider) {
    const m = providerMeta(provider);
    return (
        `<span class="provider-badge provider-${m.key}" title="${m.label} account" ` +
        `style="--provider-color:${m.color};color:${m.color}">` +
        `<span class="provider-glyph">${m.svg}</span>` +
        `<span class="provider-label">${m.label}</span></span>`
    );
}

if (typeof module !== 'undefined' && module.exports) {
    module.exports = { providerMeta, providerGlyph, providerBadge };
}
