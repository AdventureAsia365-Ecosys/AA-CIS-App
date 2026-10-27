// frontend/lib/contrast.mjs
// AA-994 S1b — WCAG 2.x contrast of the kit tokens (app/_kit/core/tokens.css), ported from AA-TripPlanner-Web
// lib/contrast.ts (AA-837, AA-995) when the token set moved into the kit core (root ADR 0016 decision 4).
// Pure functions, no imports; lib/tokensContrast.test.mjs uses them.

/** "#DB9628" / "#fff" → [r, g, b] 0-255; null for anything else (rgba(), var(), names). */
export function parseHex(value) {
  const m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(String(value).trim());
  if (!m) return null;
  const h = m[1].length === 3 ? m[1].replace(/./g, (c) => c + c) : m[1];
  return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
}

function channel(c) {
  const s = c / 255;
  return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
}

/** Relative luminance of an [r, g, b] colour. */
export function luminance([r, g, b]) {
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}

/** Contrast ratio between two hex colours (1 to 21). */
export function contrastRatio(a, b) {
  const pa = parseHex(a);
  const pb = parseHex(b);
  if (!pa || !pb) throw new Error(`not a hex colour: ${pa ? b : a}`);
  const [hi, lo] = [luminance(pa), luminance(pb)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

/** "rgba(15, 20, 27, 0.7)" → [r, g, b, a]; null for anything else. */
export function parseRgba(value) {
  const m = /^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)$/i.exec(String(value).trim());
  if (!m) return null;
  return [Number(m[1]), Number(m[2]), Number(m[3]), m[4] === undefined ? 1 : Number(m[4])];
}

/** The opaque colour a translucent scrim gives over a solid pixel (a photo's worst case is pure white under white
 *  text), as "#rrggbb" so it can go into contrastRatio. */
export function compositeOver(scrim, under) {
  const s = parseRgba(scrim);
  const u = parseHex(under);
  if (!s || !u) throw new Error(`cannot composite ${scrim} over ${under}`);
  const a = s[3];
  const mix = [0, 1, 2].map((i) => Math.round(s[i] * a + u[i] * (1 - a)));
  return `#${mix.map((c) => c.toString(16).padStart(2, "0")).join("")}`;
}

/** The `--aa-*` custom properties of each theme block of tokens.css — `:root, [data-theme="light"] { … }` and
 *  `[data-theme="dark"] { … }` — keyed without the prefix (`--aa-ink` → "ink"). Comments are ignored. */
export function readThemeTokens(css) {
  const code = css.replace(/\/\*[\s\S]*?\*\//g, "");
  const block = (selector) => {
    const m = selector.exec(code);
    if (!m) throw new Error(`theme block not found: ${selector}`);
    const body = code.slice(m.index + m[0].length, code.indexOf("}", m.index));
    const out = {};
    for (const d of body.matchAll(/--aa-([\w-]+)\s*:\s*([^;]+);/g)) out[d[1]] = d[2].trim();
    return out;
  };
  return {
    light: block(/\[data-theme="light"\]\s*\{/),
    dark: block(/\[data-theme="dark"\]\s*\{/),
  };
}
