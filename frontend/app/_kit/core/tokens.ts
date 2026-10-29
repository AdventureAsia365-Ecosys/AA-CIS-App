// UI kit core (root ADR 0016). Canonical: AA-CIS-App frontend/app/_kit/core. Edit only there; copies are checked byte for byte (kit.lock.json).
// app/_kit/core/tokens.ts
// AA-662 — UI kit shared design tokens.
//
// The kit is brand-neutral: every component reads from `K`. AA-601 part B: `K` now returns the
// `var(--aa-…)` CSS variables defined in core/tokens.css (AA-994 S1b; the app's globals.css imports it) instead of
// the raw hex, so a single <html data-theme> switch recolours every kit component. `BRAND` (brand.ts) still holds
// hex and is read directly by the login pages and the portal `T` object, which stay light.
//
// Styling convention (whole app, AA-605): inline `style={{...}}` objects reading these tokens.
// No Tailwind classNames.

import {
  BTN_PRIMARY_TEXT,
  BTN_RADIUS,
  FONT_DISPLAY,
  FONT_MONO,
  FONT_SANS,
} from "./brand";

export const K = {
  // Accent — brand gold
  accent: "var(--aa-accent)",
  accentDeep: "var(--aa-accent-deep)",
  accentSoft: "var(--aa-accent-soft)",
  accentTint: "var(--aa-accent-tint)",
  accentBorder: "var(--aa-accent-border)",
  slate: "var(--aa-slate)",
  // Semantic
  danger: "var(--aa-red)",
  dangerSoft: "var(--aa-red-soft)",
  dangerTint: "var(--aa-red-tint)",
  dangerBorder: "var(--aa-red-border)",
  success: "var(--aa-green)",
  successSoft: "var(--aa-green-soft)",
  // Amber / warning (kit-local; admin & portal each have their own slightly different amber,
  // the kit uses one consistent value).
  amber: "var(--aa-kit-amber)",
  amberSoft: "var(--aa-kit-amber-soft)",
  // Info (blue) — used by StatusBadge for "running"/"info"; neither A nor T defines a blue, so
  // the kit owns these.
  info: "var(--aa-info)",
  infoSoft: "var(--aa-info-soft)",
  // Purple — kit tone
  purple: "var(--aa-purple)",
  purpleSoft: "var(--aa-purple-soft)",
  // Neutral tone (gray pill)
  neutralBg: "var(--aa-neutral-bg)",
  neutralFg: "var(--aa-neutral-fg)",
  // Neutrals
  ink: "var(--aa-ink)",
  ink2: "var(--aa-ink2)",
  ink3: "var(--aa-ink3)",
  body: "var(--aa-body)",
  muted: "var(--aa-muted)",
  muted2: "var(--aa-muted2)",
  bg: "var(--aa-bg)",
  card: "var(--aa-card)",
  line: "var(--aa-line)",
  line2: "var(--aa-line2)",
  // Panel band (a dialog footer, a toolbar strip): one step off the card colour in both themes (AA-994 S4a, from the
  // Booking ConfirmDialog footer).
  panel: "var(--aa-panel-bg0)",
} as const;

// AA-601 part B — alpha(color, pct): a translucent version of a colour that works with CSS
// variables. The old hex-alpha-suffix trick (appending two hex digits) breaks when `color` is a
// (you cannot concatenate an alpha byte onto a `var()` string). color-mix mixes the colour with
// `transparent`, so it accepts a var() too. pct is the opacity percentage (0–100).
// e.g. alpha(A.accent, 8) → "color-mix(in srgb, var(--aa-accent) 8%, transparent)".
export function alpha(color: string, pct: number): string {
  return `color-mix(in srgb, ${color} ${pct}%, transparent)`;
}

export const serif = FONT_DISPLAY;
export const sans = FONT_SANS;
export const mono = FONT_MONO;

// ── Date formatting (AA-752) ────────────────────────────────────────────────
// One date format across the whole admin: en-GB, numeric day, short month, numeric year.
//   formatDate("2026-10-09T14:05:00Z")     -> "9 Oct 2026"
//   formatDateTime("2026-10-09T14:05:00Z") -> "9 Oct 2026, 14:05"
// null / undefined / unparseable input -> "—". A fixed locale per UI language (not the runtime locale) so a
// stored row reads the same for every admin, server-render and client-render alike: `en-GB` by default, `vi-VN`
// when the second argument is "vi" (AA-994: "9 thg 10, 2026"). In a component, `useFormat()` (core/useI18n.ts)
// binds the current UI language.
const DATE_LOCALE = { en: "en-GB", vi: "vi-VN" } as const;

export function formatDate(input: string | number | Date | null | undefined, lang: "en" | "vi" = "en"): string {
  if (input == null || input === "") return "—";
  const d = input instanceof Date ? input : new Date(input);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString(DATE_LOCALE[lang] ?? "en-GB", { day: "numeric", month: "short", year: "numeric" });
}

export function formatDateTime(input: string | number | Date | null | undefined, lang: "en" | "vi" = "en"): string {
  if (input == null || input === "") return "—";
  const d = input instanceof Date ? input : new Date(input);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString(DATE_LOCALE[lang] ?? "en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

export { BTN_RADIUS, BTN_PRIMARY_TEXT };

// Shared radii/spacing — the app had no scale object before; the kit introduces a small one so
// kit components are internally consistent. Pages keep using inline literals where they already do.
export const RADIUS = { sm: 6, md: 8, lg: 12, pill: BTN_RADIUS } as const;
export const SPACE = { xs: 4, sm: 8, md: 12, lg: 16, xl: 22 } as const;
