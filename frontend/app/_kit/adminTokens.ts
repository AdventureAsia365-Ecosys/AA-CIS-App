// app/_kit/adminTokens.ts
// CIS-local token names for the few `--aa-*` colours the admin pages draw that the kit `K` (core/tokens.ts) does not
// name (AA-1006, the end of AA-994 S6b). Root ADR 0016 decision 3 (a): an app extends the kit through its barrel with
// local parts; the values stay in core/tokens.css (both themes), only the names live here. Pages read `KX.<name>`
// instead of writing a `var(--aa-…)` string (lint rule no-raw-aa-var). Anything `K` already names is read from `K`.
// When a second app needs one of these, it moves into core `K` (promotion rule, ADR 0016 decision 3).

export const KX = {
  /** White text or icon on a solid coloured fill (a filled badge, a selected page number), in both themes. */
  onSolid: "var(--aa-on-solid)",
  /** Text on the gold accent fill (the primary button's text colour). */
  onAccent: "var(--aa-on-accent)",
  /** Text on the deliberately dark surfaces below. */
  onDark: "var(--aa-on-dark)",
  /** The fixed-dark surface of the sidebar, reused for a dark hero panel or a dark code block. */
  sideBg: "var(--aa-side-bg)",
  sidePanel: "var(--aa-side-panel)",
  /** A link blue for an external URL shown as a link. */
  link: "var(--aa-blue-link)",
  /** Neutral pill border (the grey severity chip). */
  neutralBorder: "var(--aa-neutral-border)",
  /** Amber text that reads on the soft amber fill, and its border (a warning chip or note). */
  amberDeep: "var(--aa-amber-deep)",
  amberBorder: "var(--aa-amber-border)",
  /** Green border of a success note. */
  successBorder: "var(--aa-green-border)",
  /** The flat scrim under a slide-over panel or a full-screen overlay. */
  scrim: "var(--aa-scrim)",
  /** Elevation shadows (both themes). */
  shadowSm: "var(--aa-shadow-sm)",
  shadowMd: "var(--aa-shadow-md)",
  shadowLg: "var(--aa-shadow-lg)",
} as const;
