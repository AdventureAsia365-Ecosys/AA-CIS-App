// app/_kit/tokens.ts
// AA-662 — UI kit shared design tokens.
//
// The kit is brand-neutral: every component reads from `K`, a single token object derived
// straight from `app/_brand/tokens.ts` (BRAND). It deliberately does NOT depend on the admin `A`
// or the portal `T` objects so the kit can be used from either surface without a circular import
// (adminUi.tsx / ui.tsx are the ones that re-export kit primitives behind their existing names —
// not the other way round).
//
// Styling convention (whole app, AA-605): inline `style={{...}}` objects reading these tokens.
// No Tailwind classNames.

import {
  BRAND,
  BTN_PRIMARY_TEXT,
  BTN_RADIUS,
  FONT_DISPLAY,
  FONT_MONO,
  FONT_SANS,
} from "../_brand/tokens";

export const K = {
  // Accent — brand gold
  accent: BRAND.accent,
  accentDeep: BRAND.accentDeep,
  accentSoft: BRAND.accentSoft,
  accentTint: BRAND.accentTint,
  accentBorder: BRAND.accentBorder,
  slate: BRAND.slate,
  // Semantic
  danger: BRAND.danger,
  dangerSoft: BRAND.dangerSoft,
  dangerTint: BRAND.dangerTint,
  dangerBorder: BRAND.dangerBorder,
  success: BRAND.success,
  successSoft: BRAND.successSoft,
  // Amber / warning (kit-local; admin & portal each have their own slightly different amber,
  // the kit uses one consistent value).
  amber: "#B5791F",
  amberSoft: "#FBEFD6",
  // Info (blue) — used by StatusBadge for "running"/"info"; neither A nor T defines a blue, so
  // the kit owns these.
  info: "#1E40AF",
  infoSoft: "#DBEAFE",
  // Neutrals
  ink: BRAND.ink,
  ink2: BRAND.ink2,
  ink3: BRAND.ink3,
  body: BRAND.body,
  muted: BRAND.muted,
  muted2: BRAND.muted2,
  bg: BRAND.bg,
  card: BRAND.card,
  line: BRAND.line,
  line2: BRAND.line2,
} as const;

export const serif = FONT_DISPLAY;
export const sans = FONT_SANS;
export const mono = FONT_MONO;

export { BTN_RADIUS, BTN_PRIMARY_TEXT };

// Shared radii/spacing — the app had no scale object before; the kit introduces a small one so
// kit components are internally consistent. Pages keep using inline literals where they already do.
export const RADIUS = { sm: 6, md: 8, lg: 12, pill: BTN_RADIUS } as const;
export const SPACE = { xs: 4, sm: 8, md: 12, lg: 16, xl: 22 } as const;
