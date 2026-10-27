// frontend/lib/tokensContrast.test.mjs
// AA-994 S1b (root ADR 0016 decision 4; plan guard G15): every text / background pair the kit renders meets WCAG AA
// (4.5:1, the kit's text is small) in BOTH themes of the one shared token set, app/_kit/core/tokens.css. A token PR
// that drops a checked pair under its threshold fails Frontend Build. Ported from AA-TripPlanner-Web
// lib/contrast.test.ts (AA-837, AA-995), extended with the CIS AA-760 / S248 pairs and the kit Badge tones.
// Run: npm run test:unit (CI: Frontend Build, "Kit core self-check" step).

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { compositeOver, contrastRatio, parseHex, parseRgba, readThemeTokens } from "./contrast.mjs";

const css = readFileSync(new URL("../app/_kit/core/tokens.css", import.meta.url), "utf8");
const themes = readThemeTokens(css);
const round = (x) => Math.round(x * 100) / 100;

// [text token, background token], without the --aa- prefix.
const PAIRS = [
  // Primary text on every surface (TripPlanner planner pairs + CIS table header / line2 rows).
  ["ink", "card"], ["ink", "bg"], ["ink", "accent-tint"], ["ink", "neutral-bg"], ["ink", "line"], ["ink", "line2"],
  ["ink2", "card"], ["ink3", "card"],
  ["body", "card"], ["body", "bg"], ["body", "line2"],
  // Secondary text (AA-760, S248): muted and muted2 on card, page bg, the line2 tint and the neutral pill.
  ["muted", "card"], ["muted", "bg"], ["muted", "line2"], ["muted", "neutral-bg"], ["muted", "accent-tint"],
  ["muted2", "card"], ["muted2", "bg"], ["muted2", "line2"], ["muted2", "neutral-bg"],
  // Gold text (links, active tab, accent Badge) — AA-812 D6 / AA-760.
  ["accent-deep", "card"], ["accent-deep", "bg"], ["accent-deep", "accent-tint"], ["accent-deep", "accent-soft"],
  ["accent-deep", "neutral-bg"],
  // Labels on the gold primary button, badge and avatar dots: never white on gold (AA-760).
  ["on-accent", "accent"],
  // Inverted pills.
  ["bg", "ink"], ["card", "ink"],
  // Kit Badge tones (primitives.tsx TONE) and the same colours as plain text.
  ["neutral-fg", "neutral-bg"], ["neutral-fg2", "neutral-bg"],
  ["red", "card"], ["red", "bg"], ["red", "red-soft"], ["red", "red-tint"],
  ["green", "card"], ["green", "bg"], ["green", "green-soft"],
  ["kit-amber", "card"], ["kit-amber", "bg"], ["kit-amber", "kit-amber-soft"],
  ["info", "card"], ["info", "info-soft"],
  ["purple", "card"], ["purple", "purple-soft"],
  ["slate", "card"],
  // Status pills and links of the admin `A` object.
  ["blue-link", "card"], ["green-deep", "card"], ["amber-strong", "card"], ["amber-deep", "amber-bg"],
  ["green-darkest", "green-bg"],
  // Dark panels (toast, the sidebar that is dark in both themes).
  ["on-dark", "toast-bg"], ["side-text", "side-bg"], ["side-title", "side-bg"], ["side-text2", "side-bg"],
];

// Text over a photo: the worst case is a pure-white pixel under the scrim (AA-812 D6, AA-995).
const SCRIM_PAIRS = [
  ["#FFFFFF", "scrim", 4.5], // duration pills, photo captions
];

test("contrast helpers match known WCAG values", () => {
  assert.equal(round(contrastRatio("#000000", "#ffffff")), 21);
  assert.equal(round(contrastRatio("#DB9628", "#FFFFFF")), 2.5); // the old white label on the gold button
  assert.equal(round(contrastRatio("#9E6E16", "#FBF3E3")), 4.05); // the old accent-deep on accent-tint
  assert.equal(round(contrastRatio("#6B7380", "#F8F6F2")), 4.43); // the old light muted on the page background
  assert.equal(parseHex("rgba(0,0,0,0.5)"), null);
  assert.deepEqual(parseRgba("rgba(15, 20, 27, 0.68)"), [15, 20, 27, 0.68]);
  assert.equal(compositeOver("rgba(0, 0, 0, 0.5)", "#FFFFFF"), "#808080");
  // TripPlanner's original .55 scrim is not AA-safe for white text (S252): about 4.2:1.
  assert.ok(contrastRatio("#FFFFFF", compositeOver("rgba(15, 20, 27, 0.55)", "#FFFFFF")) < 4.5);
});

test("both theme blocks define the same tokens (a token missing in dark would silently keep its light value)", () => {
  const light = Object.keys(themes.light).sort();
  const dark = Object.keys(themes.dark).sort();
  assert.ok(light.length > 80, `light block parsed: ${light.length} tokens`);
  assert.deepEqual(light.filter((k) => !(k in themes.dark)), []);
  assert.deepEqual(dark.filter((k) => !(k in themes.light)), []);
});

for (const theme of ["light", "dark"]) {
  test(`every checked text pair meets WCAG AA (4.5:1) in the ${theme} theme`, () => {
    const t = themes[theme];
    const failures = PAIRS.map(([fg, bg]) => {
      assert.ok(t[fg] && t[bg], `token missing in ${theme}: --aa-${t[fg] ? bg : fg}`);
      return { pair: `--aa-${fg} ${t[fg]} on --aa-${bg} ${t[bg]}`, ratio: round(contrastRatio(t[fg], t[bg])) };
    }).filter((r) => r.ratio < 4.5);
    assert.deepEqual(failures, []);
  });

  test(`text on the photo scrim is readable over a white pixel in the ${theme} theme`, () => {
    const t = themes[theme];
    const failures = SCRIM_PAIRS.map(([fg, scrim, min]) => {
      assert.ok(t[scrim] && parseRgba(t[scrim]), `scrim missing or not rgba in ${theme}: ${scrim}`);
      const text = fg.startsWith("#") ? fg : t[fg];
      return { pair: `${fg} on ${scrim}`, ratio: round(contrastRatio(text, compositeOver(t[scrim], "#FFFFFF"))), min };
    }).filter((r) => r.ratio < r.min);
    assert.deepEqual(failures, []);
  });

  test(`muted2 stays the quieter tone and the hero fallback is dark in the ${theme} theme`, () => {
    const t = themes[theme];
    assert.ok(contrastRatio(t.muted, t.card) >= contrastRatio(t.muted2, t.card), "muted2 must not outshine muted");
    assert.ok(contrastRatio("#FFFFFF", t["hero-bg"]) >= 4.5, "white hero text on --aa-hero-bg");
  });
}

test("the font tokens use the next/font variable with the brand family as fallback", () => {
  for (const [name, family] of [["sans", "Poppins"], ["display", "Fahkwang"], ["mono", "JetBrains Mono"]]) {
    assert.match(css, new RegExp(`--aa-font-${name}:\\s*var\\(--font-[\\w-]+, "${family}"\\)`), name);
  }
});
