// frontend/lib/kitBrandTheme.test.mjs
// AA-1038 R5 slice 3 — the runtime brand-token layer (app/_kit/core/brandTheme.ts, root ADR 0016 amendment): a tenant
// theme (kit token -> #rrggbb) becomes one CSS block over the `--aa-*` variables of tokens.css. Run: npm run test:unit.
//
// Checks: the kit values brandTheme.ts checks against equal tokens.css; only the accent family is themable and only
// hex values count; a readable tenant accent is applied with a picked label colour; a pair under its WCAG threshold
// drops the tenant's token (the kit value comes back); the dark theme takes the accent only when it keeps 3:1 on the
// dark card; the CSS block has no quote, angle bracket or ampersand (it renders as <style> text) and is empty for no
// theme; the file is in the tokens profile (TripPlanner gets it with tokens.css) and imports nothing.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { BRAND_PAIRS, BRAND_TOKENS, KIT_DARK_CARD, KIT_LIGHT, brandThemeCss, contrast, onColour,
  resolveBrandTheme } from "../app/_kit/core/brandTheme.ts";
import { TOKENS_PROFILE } from "../app/_kit/core/check-kit-copy.mjs";
import { readThemeTokens } from "./contrast.mjs";

const css = readFileSync(new URL("../app/_kit/core/tokens.css", import.meta.url), "utf8");
const themes = readThemeTokens(css);

test("the kit values brandTheme.ts checks against are tokens.css's", () => {
  for (const [token, value] of Object.entries(KIT_LIGHT)) {
    assert.equal(themes.light[token].toLowerCase(), value, token);
  }
  assert.equal(themes.dark.card.toLowerCase(), KIT_DARK_CARD);
  for (const token of BRAND_TOKENS) assert.ok(token in themes.light, `${token} is a kit token`);
});

test("the kit defaults themselves pass every brand pair", () => {
  for (const [fg, bg, min] of BRAND_PAIRS) {
    assert.ok(contrast(KIT_LIGHT[fg], KIT_LIGHT[bg]) >= min, `${fg} on ${bg}`);
  }
});

test("only the accent family and hex values count", () => {
  const r = resolveBrandTheme({ accent: "#1D4E89", ink: "#000000", "accent-deep": "navy", card: "#ff0000" });
  assert.deepEqual(Object.keys(r.light).sort(), ["accent", "on-accent"]);
  assert.equal(r.light.accent, "#1d4e89");
  assert.deepEqual(r.dropped.map((d) => [d.token, d.reason]).sort(), [
    ["accent-deep", "not_a_colour"], ["card", "not_themable"], ["ink", "not_themable"]]);
  for (const bad of [null, [], "theme", 3, {}]) assert.equal(brandThemeCss(bad), "");
});

test("a readable accent gets a label colour that reads on it", () => {
  const dark = resolveBrandTheme({ accent: "#1d4e89" });
  assert.equal(dark.light["on-accent"], "#ffffff");
  const light = resolveBrandTheme({ accent: "#f2c94c" });
  assert.equal(light.light["on-accent"], "#1a1206");
  assert.equal(onColour("#000000"), "#ffffff");
});

test("a pair under its threshold drops the tenant's token", () => {
  // A pale accent-deep is unreadable as text on the card: dropped, the kit value comes back.
  const r = resolveBrandTheme({ accent: "#1d4e89", "accent-deep": "#f0f0f0" });
  assert.equal(r.light["accent-deep"], undefined);
  assert.ok(r.dropped.some((d) => d.token === "accent-deep" && d.reason.startsWith("contrast_")));
  // A pale accent-tint behind the kit's gold text: the tint is dropped (accent-deep on accent-tint).
  const tint = resolveBrandTheme({ "accent-tint": "#5a4a2a" });
  assert.equal(tint.light["accent-tint"], undefined);
  // A label the tenant set that does not read on its accent is replaced by the picked one; the accent stays.
  const label = resolveBrandTheme({ accent: "#1d4e89", "on-accent": "#203040" });
  assert.equal(label.light["on-accent"], "#ffffff");
  assert.equal(label.light.accent, "#1d4e89");
  assert.ok(label.dropped.some((d) => d.token === "on-accent"));
});

test("the dark theme takes the accent only when it keeps 3:1 on the dark card", () => {
  const bright = resolveBrandTheme({ accent: "#e07a1f" });
  assert.equal(bright.dark.accent, "#e07a1f");
  assert.ok(contrast(bright.dark["on-accent"], bright.dark.accent) >= 4.5);
  const navy = resolveBrandTheme({ accent: "#1d4e89" });
  assert.deepEqual(navy.dark, {});
});

test("the CSS block is safe <style> text and wins over tokens.css", () => {
  const out = brandThemeCss({ accent: "#e07a1f", "accent-soft": "#fde7d2", "accent-tint": "#fff6ee" });
  assert.match(out, /^html:not\(\[data-theme\]\) body,html\[data-theme=light\] body\{--aa-accent:#e07a1f;/);
  assert.match(out, /html\[data-theme=dark\] body\{--aa-accent:#e07a1f;--aa-on-accent:#[0-9a-f]{6};\}$/);
  assert.doesNotMatch(out, /["'<>&]/);
});

test("brandTheme.ts is a tokens-profile file and imports nothing", () => {
  assert.ok(TOKENS_PROFILE.includes("brandTheme.ts"));
  const src = readFileSync(new URL("../app/_kit/core/brandTheme.ts", import.meta.url), "utf8");
  assert.doesNotMatch(src, /^\s*import\s/m);
  assert.ok(src.startsWith("// UI kit core (root ADR 0016)."));
});
