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
            color: '#60a5fa',       // blue — chosen to avoid the green/amber/red
            labelColor: '#93c5fd',  // usage palette; light shade clears WCAG AA 4.5:1
            // Enclosed hexagon ring: a CLOSED polygon silhouette, deliberately
            // distinct from Claude's open radial spokes so the two are
            // distinguishable by SHAPE alone (color-blind safe), not just color.
            svg:
                '<svg viewBox="0 0 16 16" width="100%" height="100%" fill="none" ' +
                'stroke="currentColor" stroke-width="1.4" stroke-linejoin="round" aria-hidden="true">' +
                '<polygon points="8,1.6 13.5,4.8 13.5,11.2 8,14.4 2.5,11.2 2.5,4.8"/>' +
                '<circle cx="8" cy="8" r="1.9"/>' +
                '</svg>',
        };
    }
    return {
        key: 'claude',
        label: 'Claude',
        color: '#a78bfa',       // violet — NOT orange (orange reads as a usage
        labelColor: '#c4b5fd',  // warning here); light shade clears WCAG AA 4.5:1
        // Anthropic-style sunburst: open radial spokes from center.
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

/** Full badge (logo + label chip) — for the roomier dashboard cards. The glyph
 * keeps the full brand color (a graphical object, 3:1 is enough); the label text
 * uses the lighter labelColor so it clears WCAG AA 4.5:1 on the tinted chip. */
function providerBadge(provider) {
    const m = providerMeta(provider);
    return (
        `<span class="provider-badge provider-${m.key}" title="${m.label} account" ` +
        `style="--provider-color:${m.color}">` +
        `<span class="provider-glyph" style="color:${m.color}">${m.svg}</span>` +
        `<span class="provider-label" style="color:${m.labelColor}">${m.label}</span></span>`
    );
}

if (typeof module !== 'undefined' && module.exports) {
    module.exports = { providerMeta, providerGlyph, providerBadge };
}
