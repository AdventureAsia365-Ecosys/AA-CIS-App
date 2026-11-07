// UI kit core (root ADR 0016). Canonical: AA-CIS-App frontend/app/_kit/core. Edit only there; copies are checked byte for byte (kit.lock.json).
// app/_kit/core/brandTheme.ts — the runtime brand-token layer of the kit (AA-1038 R5 slice 3, root ADR 0019 d10 and the
// ADR 0016 amendment "a runtime brand-token layer built once in the CIS kit core and synced").
//
// A tenant's site settings carry a theme: an object of kit token name -> "#rrggbb" (CIS shared.tenant_tripplanner_site,
// served by catalog.v_tenant_tripplanner_v1; the CIS CHECK allows token-shaped names and lower-case hex only). This
// module turns it into one CSS block that overrides the `--aa-*` variables of tokens.css for that page only, so every
// kit component and every k-* class follows the tenant's brand with no per-page variant. Pure and import-free (it is
// in the `tokens` profile, so TripPlanner gets it with tokens.css).
//
// Rules:
//   - Only the accent family is themable (BRAND_TOKENS): the neutrals, text and semantic colours stay the kit's, so the
//     contrast pairs of tokens.css (lib/tokensContrast.test.mjs) still hold for everything a tenant cannot change.
//   - A value is used only when it is "#rrggbb" (lower or upper case, normalised to lower). Anything else is dropped.
//   - The light theme gets every themable token; then each WCAG pair below is checked against the kit's light
//     surfaces (card, bg) and the tenant's own colours: a pair under its threshold drops the tenant's text token (the
//     kit value comes back), and when that is not enough, the tenant's background token. `on-accent` is picked for
//     contrast (the kit's dark ink or white) when the tenant did not set one or set one that does not read on its
//     accent. The accent against the page has no pair: the kit's own gold is under 3:1 on white and its buttons and
//     focus rings carry their own contrast (borders, accent-deep), as in tokens.css.
//   - The dark theme takes the tenant's `accent` (and its picked `on-accent`) only when the accent keeps 3:1 against
//     the kit's dark card (a button, WCAG 1.4.11); the deep / soft / tint / border tokens stay the kit's dark values.
//   - Output selectors name the theme the restore script writes on <html data-theme>, plus the no-script default,
//     with one more element than tokens.css so the block wins whatever order the stylesheets load in. The block has no
//     quote, angle bracket or ampersand, so it renders as the text of a <style> element as it is.

/** The kit tokens a tenant theme may set (names as in tokens.css without `--aa-`). */
export const BRAND_TOKENS = ["accent", "accent-deep", "accent-soft", "accent-tint", "accent-border", "on-accent"] as const;
export type BrandToken = (typeof BRAND_TOKENS)[number];

/** The kit's values this module checks against (tokens.css; lib/kitBrandTheme.test.mjs keeps them equal). */
export const KIT_LIGHT: Record<BrandToken | "card" | "bg", string> = {
  accent: "#db9628",
  "accent-deep": "#875d12",
  "accent-soft": "#f4e2c2",
  "accent-tint": "#fbf3e3",
  "accent-border": "#dda64e",
  "on-accent": "#1a1206",
  card: "#ffffff",
  bg: "#f8f6f2",
};
export const KIT_DARK_CARD = "#19212c";
const INK = "#1a1206";
const WHITE = "#ffffff";

/** [text-like token, background token, minimum ratio]: what the kit draws with these tokens. */
export const BRAND_PAIRS: ReadonlyArray<readonly [string, string, number]> = [
  ["on-accent", "accent", 4.5],        // labels on the primary button and badges
  ["accent-deep", "card", 4.5],        // links, active tab, gold text
  ["accent-deep", "bg", 4.5],
  ["accent-deep", "accent-tint", 4.5],
  ["accent-deep", "accent-soft", 4.5],
];

const HEX = /^#[0-9a-fA-F]{6}$/;

function channel(c: number): number {
  const s = c / 255;
  return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
}

function luminance(hex: string): number {
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}

/** WCAG 2.x contrast ratio of two "#rrggbb" colours (1 to 21). */
export function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

/** The label colour for a button of `background`: the kit's dark ink or white, whichever contrasts more. */
export function onColour(background: string): string {
  return contrast(INK, background) >= contrast(WHITE, background) ? INK : WHITE;
}

export interface BrandTheme {
  /** Light-theme overrides that passed every pair (token -> "#rrggbb"). */
  light: Partial<Record<BrandToken, string>>;
  /** Dark-theme overrides (the accent and its label only, when readable on the dark card). */
  dark: Partial<Record<BrandToken, string>>;
  /** Tenant tokens dropped, with why (an unknown name, not a colour, or a contrast pair). Never shown to travellers. */
  dropped: Array<{ token: string; reason: string }>;
}

/** The checked overrides of a tenant theme (any JSON value; only an object of known tokens -> hex counts). */
export function resolveBrandTheme(theme: unknown): BrandTheme {
  const dropped: BrandTheme["dropped"] = [];
  const given: Partial<Record<BrandToken, string>> = {};
  if (theme && typeof theme === "object" && !Array.isArray(theme)) {
    for (const [key, value] of Object.entries(theme as Record<string, unknown>)) {
      if (!(BRAND_TOKENS as readonly string[]).includes(key)) {
        dropped.push({ token: key, reason: "not_themable" });
      } else if (typeof value !== "string" || !HEX.test(value)) {
        dropped.push({ token: key, reason: "not_a_colour" });
      } else {
        given[key as BrandToken] = value.toLowerCase();
      }
    }
  }
  const light: Partial<Record<BrandToken, string>> = { ...given };
  if (light.accent && !given["on-accent"]) light["on-accent"] = onColour(light.accent);
  const value = (t: string) => (light as Record<string, string>)[t] ?? (KIT_LIGHT as Record<string, string>)[t];
  for (let round = 0; round < BRAND_PAIRS.length + 1; round += 1) {
    let changed = false;
    for (const [fg, bgToken, min] of BRAND_PAIRS) {
      if (contrast(value(fg), value(bgToken)) >= min) continue;
      if (fg === "on-accent" && light.accent && light["on-accent"] !== onColour(light.accent)) {
        // The tenant's label does not read on its accent: the picked label replaces it, the accent stays.
        light["on-accent"] = onColour(light.accent);
        dropped.push({ token: fg, reason: `contrast_${fg}_on_${bgToken}` });
        changed = true;
        continue;
      }
      const drop = fg in light ? fg : bgToken in light ? bgToken : null;
      if (!drop) continue;
      delete (light as Record<string, string>)[drop];
      dropped.push({ token: drop, reason: `contrast_${fg}_on_${bgToken}` });
      if (drop === "accent" && !given["on-accent"]) delete light["on-accent"];
      changed = true;
    }
    if (!changed) break;
  }
  const dark: Partial<Record<BrandToken, string>> = {};
  if (light.accent && contrast(light.accent, KIT_DARK_CARD) >= 3) {
    dark.accent = light.accent;
    dark["on-accent"] = given["on-accent"] && contrast(given["on-accent"], light.accent) >= 4.5
      ? given["on-accent"]
      : onColour(light.accent);
  }
  return { light, dark, dropped };
}

function declarations(values: Partial<Record<BrandToken, string>>): string {
  return BRAND_TOKENS.filter((t) => values[t]).map((t) => `--aa-${t}:${values[t]};`).join("");
}

/** One CSS block for a <style> element (empty string when nothing applies). */
export function brandThemeCss(theme: unknown): string {
  const { light, dark } = resolveBrandTheme(theme);
  const lightDecl = declarations(light);
  const darkDecl = declarations(dark);
  let css = "";
  if (lightDecl) css += `html:not([data-theme]) body,html[data-theme=light] body{${lightDecl}}`;
  if (darkDecl) css += `html[data-theme=dark] body{${darkDecl}}`;
  return css;
}
