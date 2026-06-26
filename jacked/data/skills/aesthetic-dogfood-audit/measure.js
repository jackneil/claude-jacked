/**
 * Aesthetic dogfood audit — in-page MEASURE script.
 *
 * Paste the body into the browser's evaluate-script / console tool (it returns a
 * JSON report). It catches what eyeballing misses. Read every flagged item against
 * the design bar — and against the FALSE-POSITIVE notes below before flagging.
 *
 * FALSE POSITIVES (verify before reporting any of these):
 *   - overflowRightEls inside a `.overflow-x-auto` ancestor → in-container scroll, fine.
 *   - clippedTables: only real if parentOverflowX is NOT auto/scroll (else it scrolls).
 *   - white-ish text on a DARK background → correct (dark theme), not invisible.
 *   - a child inset by card padding reads as "misaligned" → compare top-level siblings.
 *   - SVG chart axis labels show as non-tabular / odd sizes → expected, ignore.
 */
() => {
  const root = document.querySelector('main') || document.body;
  const de = document.documentElement;
  const vw = de.clientWidth;
  const R = (e) => e.getBoundingClientRect();
  const vis = (e) => { const r = R(e); return r.width > 0 && r.height > 0; };
  const T = (e) => (e.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 22);
  const lum = (c) => { const m = c.match(/(\d+), (\d+), (\d+)/); if (!m) return null;
    const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
    return 0.2126 * f(+m[1]) + 0.7152 * f(+m[2]) + 0.0722 * f(+m[3]); };

  // 1. TYPE-SCALE SPRAWL — distinct font-size/weight on text leaves (>~6 = sprawl)
  const type = new Map();
  root.querySelectorAll('*').forEach((e) => {
    if (e.children.length === 0 && e.textContent.trim() && vis(e)) {
      const cs = getComputedStyle(e);
      const k = `${Math.round(parseFloat(cs.fontSize))}px/${cs.fontWeight}`;
      type.set(k, (type.get(k) || 0) + 1);
    }
  });

  // 2. SPACING DUPES — near-duplicate gap values on flex/grid containers
  const gaps = new Map();
  root.querySelectorAll('*').forEach((e) => {
    const cs = getComputedStyle(e);
    if ((cs.display.includes('flex') || cs.display.includes('grid')) && vis(e)) {
      const g = cs.gap || cs.columnGap;
      if (g && g !== 'normal' && g !== '0px') gaps.set(g, (gaps.get(g) || 0) + 1);
    }
  });

  // 3. EDGE MISALIGNMENT — left-x of TOP-LEVEL sibling cards (children excluded)
  //   Swap `.theme-card`/`.theme-card-padded` for YOUR app's card class if you have
  //   one; the generic `[class*=rounded][class*=shadow]` / `[class*=rounded][class*=border]`
  //   fallbacks already match Tailwind/utility-class apps, so this works untouched on most.
  const cards = [...root.querySelectorAll('.theme-card, .theme-card-padded, [class*=rounded][class*=shadow], [class*=rounded][class*=border]')]
    .filter((e) => vis(e) && R(e).width > 250 && !e.parentElement.closest('[class*=rounded][class*=shadow],[class*=rounded][class*=border]'));
  const lefts = {};
  cards.forEach((c) => { const x = Math.round(R(c).left); lefts[x] = (lefts[x] || 0) + 1; });

  // 4. CURRENCY not tabular / not right-aligned (only matters inside a column/table)
  const money = [...root.querySelectorAll('td, p, span, div, dd')]
    .filter((e) => e.children.length === 0 && /^\$?[\d,]+\.\d{2}$|^\$[\d,]+$/.test((e.textContent || '').trim()) && vis(e));
  const moneyNonTabular = money.filter((e) => !getComputedStyle(e).fontVariantNumeric.includes('tabular')).length;

  // 5. RAW VALUES leaking to the UI
  const txt = root.innerText || '';
  const raw = [];
  if (/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}/i.test(txt)) raw.push('UUID');
  if (/\bNaN\b/.test(txt)) raw.push('NaN');
  if (/(\$undefined|: ?undefined|>undefined<)/.test(txt)) raw.push('undefined');
  if (/\[object /.test(txt)) raw.push('[object]');

  // 6. RAGGED WRAPS — short labels/badges/buttons rendered ~2-3 line-heights tall
  const wrapped = [...root.querySelectorAll('span, button, a, th, p, label')]
    .filter((e) => { if (!vis(e) || e.children.length) return false;
      const cs = getComputedStyle(e); const lh = parseFloat(cs.lineHeight) || parseFloat(cs.fontSize) * 1.3;
      return R(e).height > lh * 1.7 && (e.textContent || '').trim().length < 26 && R(e).width < 220; })
    .map(T);

  // 7. CONTRAST — text vs background luminance ratio < 4.5 (AA), light surfaces only
  const lowContrast = [];
  root.querySelectorAll('*').forEach((e) => {
    if (e.children.length === 0 && e.textContent.trim() && vis(e)) {
      const cs = getComputedStyle(e);
      let bg = cs.backgroundColor, p = e;
      while ((bg === 'rgba(0, 0, 0, 0)' || bg === 'transparent') && p.parentElement) { p = p.parentElement; bg = getComputedStyle(p).backgroundColor; }
      const lt = lum(cs.color), lb = lum(bg);
      if (lt != null && lb != null) { const ratio = (Math.max(lt, lb) + 0.05) / (Math.min(lt, lb) + 0.05);
        if (ratio < 4.5 && R(e).height >= 10) lowContrast.push(`${T(e)} (${ratio.toFixed(1)}:1)`); }
    }
  });

  // 8. OVERFLOW / PAGE SCROLL / CLIPPED TABLES / TAP GAPS
  const overflowRight = [...root.querySelectorAll('*')]
    .filter((e) => { if (e.closest('[class*=overflow-x]')) return false; const r = R(e);
      return r.right > vw + 2 && r.width > 0 && r.width < vw && e.textContent.trim(); })
    .map(T);
  const tables = [...document.querySelectorAll('table')].filter((t) => vis(t)).map((t) => {
    const ox = getComputedStyle(t.parentElement).overflowX;
    return { cols: t.querySelectorAll('thead th').length, parentOverflowX: ox,
      clipped: t.scrollWidth > t.parentElement.clientWidth + 2 && !/auto|scroll/.test(ox) };
  });
  // tap targets closer than 8px (mobile)
  const taps = [...root.querySelectorAll('button, a')].filter(vis);
  let tightTaps = 0;
  for (let i = 0; i < taps.length; i++) for (let j = i + 1; j < taps.length; j++) {
    const a = R(taps[i]), b = R(taps[j]);
    if (Math.abs(a.top - b.top) < 4 && a.right <= b.left && b.left - a.right < 8) tightTaps++;
  }

  return {
    width: window.innerWidth,
    pageScrollsSideways: de.scrollWidth > vw + 1,
    typeScale: { distinct: type.size, styles: [...type.entries()].sort((a, b) => b[1] - a[1]).slice(0, 12), SPRAWL: type.size > 6 },
    spacingValues: [...gaps.entries()].sort((a, b) => b[1] - a[1]),
    cardLeftEdges: lefts, MISALIGNED: Object.keys(lefts).length > 1,
    money: { count: money.length, nonTabular: moneyNonTabular },
    rawValuesLeaking: raw,
    raggedWraps: [...new Set(wrapped)].slice(0, 8),
    lowContrast: [...new Set(lowContrast)].slice(0, 8),
    overflowRight: [...new Set(overflowRight)].slice(0, 8),
    tables,
    clippedTables: tables.filter((t) => t.clipped).length,
    tightTapTargets: tightTaps,
  };
}
