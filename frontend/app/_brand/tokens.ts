// app/_brand/tokens.ts
// AA-605 — single source of Adventure Asia brand tokens for BOTH the admin UI
// (admin/_components/adminUi.tsx, `A`) and the tenant portal (portal/_components/ui.tsx, `T`).
// Values come from the live adventure.asia site (audited 24/09/2026): gold primary button
// #DB9628 -> hover #9E6E16, outline border #DDA64E, text #33363D, cream bg #F8F6F2, slate
// secondary #35495E, alert red #C20101; Fahkwang headings + Poppins body; pill buttons.
// `A` and `T` keep their own key names (hundreds of call sites) and read values from here, so a
// brand change is made once, in this file.

export const BRAND = {
  // Accent — brand gold (logo, primary buttons, active nav/tab, spinners)
  accent:       "#DB9628",
  accentDeep:   "#9E6E16", // hover / text on gold tint
  accentSoft:   "#F4E2C2",
  accentTint:   "#FBF3E3",
  accentBorder: "#DDA64E",
  // Secondary — brand slate (neutral category colour, never an error)
  slate:        "#35495E",
  // Semantic — kept separate from the accent
  danger:       "#C20101",
  dangerSoft:   "#FBE9E7",
  dangerTint:   "#FDF4F3",
  dangerBorder: "#F2C4BF",
  success:      "#2E7D5B",
  successSoft:  "#E4F1E9",
  // Neutrals (already shared by admin, portal and adventure.asia)
  ink:    "#1F2933",
  ink2:   "#2A333E",
  ink3:   "#3A4453",
  body:   "#33363D",
  muted:  "#6B7380",
  muted2: "#9099A6",
  bg:     "#F8F6F2",
  card:   "#FFFFFF",
  line:   "#E9E4DB",
  line2:  "#F0EBE0",
} as const;

// Fonts are loaded once in app/layout.tsx (Google Fonts <link>).
export const FONT_DISPLAY = "'Fahkwang', 'Poppins', Georgia, serif"; // titles, big numbers
export const FONT_SANS    = "'Poppins', system-ui, -apple-system, sans-serif"; // UI + body
export const FONT_MONO    = "'JetBrains Mono', 'IBM Plex Mono', monospace"; // IDs, codes

// Buttons are pills on adventure.asia; the primary one is uppercase with light tracking.
export const BTN_RADIUS = 999;
export const BTN_PRIMARY_TEXT: { textTransform: "uppercase"; letterSpacing: string } = {
  textTransform: "uppercase", letterSpacing: "0.06em",
};

export const LOGO_SRC = "/brand/adventure-asia-logo.png";
