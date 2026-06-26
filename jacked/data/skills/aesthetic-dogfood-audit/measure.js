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
 *   - a11y.focusRingCandidates: programmatic focus may not trip :focus-visible, and a ring
 *       drawn on a PARENT or via :focus-within/background swap isn't detected → these are
 *       candidates, confirm with the keyboard Tab walk. If hasGlobalFocusStyle is false the
 *       app has no focus ring at all → real Blocker.
 *   - smallTouchTargets: inline text links inside prose aren't tap targets; judge
 *       standalone buttons/links/controls, not every <a> in a paragraph.
 *   - colorOnlyStatus: a purely decorative red/green dot (not conveying status) is a FP.
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

  // 1. TYPE-SCALE SPRAWL — distinct font-size/weight on text leaves (>~6 = sprawl),
  //    plus distinct font-FAMILY count (>3 distinct families = font drift).
  const type = new Map();
  const fam = new Map();
  root.querySelectorAll('*').forEach((e) => {
    if (e.children.length === 0 && e.textContent.trim() && vis(e)) {
      const cs = getComputedStyle(e);
      const k = `${Math.round(parseFloat(cs.fontSize))}px/${cs.fontWeight}`;
      type.set(k, (type.get(k) || 0) + 1);
      const ff = (cs.fontFamily.split(',')[0] || '').replace(/["']/g, '').trim().toLowerCase();
      if (ff) fam.set(ff, (fam.get(ff) || 0) + 1);
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

  // 9. A11Y — FOCUS VISIBILITY. Does the app define ANY :focus/:focus-visible ring in CSS,
  //    and which focusables show no visible indicator when focused? (See false-positive note.)
  const hasGlobalFocusStyle = (() => {
    try {
      for (const ss of document.styleSheets) {
        let rules; try { rules = ss.cssRules; } catch (_) { continue; }
        if (!rules) continue;
        for (const r of rules) {
          const sel = r.selectorText;
          if (sel && /:focus(-visible)?/.test(sel) && r.style) {
            const o = r.style.outline, ow = r.style.outlineWidth, bs = r.style.boxShadow;
            if ((o && o !== 'none' && o !== '0') || (ow && ow !== '0px' && ow !== '0') || (bs && bs !== 'none')) return true;
          }
        }
      }
    } catch (_) {}
    return false;
  })();
  const focusSel = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"]), [role=button]';
  const focusables = [...root.querySelectorAll(focusSel)].filter(vis);
  const prevFocus = document.activeElement;
  const noFocusRing = [];
  focusables.slice(0, 80).forEach((e) => {
    const before = getComputedStyle(e).boxShadow;
    try { e.focus({ preventScroll: true }); } catch (_) { return; }
    const cs = getComputedStyle(e);
    const outline = cs.outlineStyle !== 'none' && parseFloat(cs.outlineWidth) !== 0;
    const shadow = cs.boxShadow !== 'none' && cs.boxShadow !== before;
    let fv = false; try { fv = e.matches(':focus-visible'); } catch (_) {}
    if (!outline && !shadow && !fv) noFocusRing.push(T(e) || e.tagName.toLowerCase());
  });
  try { if (prevFocus && typeof prevFocus.focus === 'function') prevFocus.focus({ preventScroll: true }); } catch (_) {}

  // 10. TOUCH-TARGET SIZE — interactive controls smaller than 44×44 (mobile fit). Pairs
  //     with the 8px-proximity check above; judge standalone controls, not inline prose links.
  const smallTargets = [...root.querySelectorAll('button, a, input, select, textarea, [role=button]')]
    .filter((e) => { const r = R(e); return r.width > 0 && r.height > 0 && (r.width < 44 || r.height < 44); })
    .map((e) => `${T(e) || e.tagName.toLowerCase()} (${Math.round(R(e).width)}×${Math.round(R(e).height)})`);

  // 11. MOTION — no `transition: all`; animate transform/opacity, never layout props
  //     (width/height/top/left/margin jank); honor prefers-reduced-motion.
  const LAYOUT_PROPS = ['width', 'height', 'top', 'left', 'right', 'bottom', 'margin', 'margin-top', 'margin-right', 'margin-bottom', 'margin-left'];
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const motionAll = []; const motionLayout = []; let stillAnimating = 0;
  root.querySelectorAll('*').forEach((e) => {
    if (!vis(e)) return;
    const cs = getComputedStyle(e);
    const tp = cs.transitionProperty;
    if (tp === 'all') motionAll.push(T(e) || e.tagName.toLowerCase());
    else if (tp && tp !== 'none') {
      const hit = tp.split(',').map((s) => s.trim()).filter((p) => LAYOUT_PROPS.includes(p));
      if (hit.length) motionLayout.push(`${T(e) || e.tagName.toLowerCase()} (${hit.join('/')})`);
    }
    if (reduceMotion) {
      const animating = (cs.transitionDuration && /[1-9]/.test(cs.transitionDuration)) || (cs.animationName && cs.animationName !== 'none');
      if (animating) stillAnimating++;
    }
  });

  // 12. COPY HYGIENE + COLOR-ONLY STATUS
  //     - typographic polish: literal "..." (want "…"), straight ' / " (want curly ’ “ ”).
  //     - color-only status: a small red/green dot/badge with no text label and no icon
  //       fails for ~8% colorblind users — pair the color with an icon or word.
  const copyHygiene = {
    literalEllipsis: (txt.match(/\.\.\./g) || []).length,
    straightApostrophes: (txt.match(/[A-Za-z]'[A-Za-z]/g) || []).length,
    // Only count straight double-quotes used as QUOTATION marks: a quote wrapping a letter,
    // but NOT one touching a digit — so inch marks / dimensions (5"x7", 12") and code don't
    // inflate the count, while real prose quotes ("hello") still register.
    straightQuotes: (txt.match(/(?<!\d)"(?=[A-Za-z])|(?<=[A-Za-z])"(?!\d)/g) || []).length,
  };
  // Red OR green with clear hue dominance — exclude neutral grays (r≈g≈b), which would
  // otherwise match the green branch (e.g. rgb(128,128,128)).
  const isRedGreen = (c) => { const m = c.match(/(\d+), (\d+), (\d+)/); if (!m) return false;
    const r = +m[1], g = +m[2], b = +m[3];
    return (r > 140 && r - g > 30 && r - b > 30) || (g > 110 && g - r > 25 && g - b > 25); };
  const colorOnlyStatus = [...root.querySelectorAll('span, div, i, [class*=badge], [class*=status], [class*=dot], [class*=indicator]')]
    .filter((e) => { if (!vis(e)) return false; const r = R(e);
      if (r.width > 28 || r.height > 28) return false;
      if ((e.textContent || '').trim()) return false;
      if (e.querySelector('svg, img, [class*=icon]')) return false;
      const cs = getComputedStyle(e); return isRedGreen(cs.backgroundColor) || isRedGreen(cs.color); })
    .length;

  return {
    width: window.innerWidth,
    pageScrollsSideways: de.scrollWidth > vw + 1,
    typeScale: { distinct: type.size, styles: [...type.entries()].sort((a, b) => b[1] - a[1]).slice(0, 12), SPRAWL: type.size > 6,
      fontFamilies: [...fam.keys()], familyCount: fam.size, FAMILY_DRIFT: fam.size > 3 },
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
    smallTouchTargets: { count: smallTargets.length, sample: [...new Set(smallTargets)].slice(0, 8) },
    a11y: { focusables: focusables.length, hasGlobalFocusStyle, focusRingMissingCount: noFocusRing.length, focusRingCandidates: [...new Set(noFocusRing)].slice(0, 8) },
    motion: { transitionAll: [...new Set(motionAll)].slice(0, 8), animatingLayoutProps: [...new Set(motionLayout)].slice(0, 8), reducedMotionActive: reduceMotion, stillAnimatingUnderReduce: stillAnimating },
    copyHygiene,
    colorOnlyStatus,
  };
}
