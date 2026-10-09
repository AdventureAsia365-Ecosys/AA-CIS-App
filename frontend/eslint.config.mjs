import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

// AA-732 — UI-kit conventions enforced in admin + portal page code (no new dependency; core
// `no-restricted-syntax` only).
//
// Rules (see CONTEXT.md "UI kit"):
//   1. no raw <table>        → use the kit DataTable
//   2. no hand-rolled modal  → use the kit Modal / Drawer (proxied by `role="dialog"` on a raw
//                              element outside the kit)
//   3. no `className` on JSX → UI v2 is inline style + brand tokens, not Tailwind classes
//
// Severity policy (brief): files already migrated onto the kit are held to `error`; legacy files
// not yet migrated are `warn` so `npm run lint` stays green and no legacy page is rewritten.
// Flat config is last-match-wins per rule, so the broad `warn` block comes first and the
// specific `error` block for already-clean kit files comes after.
//
// The kit itself (app/_kit/**) is exempt: DataTable renders the one <table>, Modal/Drawer own the
// one role="dialog".
const KIT_RESTRICTED_SYNTAX = [
  {
    selector: "JSXOpeningElement[name.name='table']",
    message:
      "No raw <table> in page code — use the kit DataTable (app/_kit). (AA-732 kit rule)",
  },
  {
    selector: "JSXAttribute[name.name='role'][value.value='dialog']",
    message:
      "No hand-rolled modal/dialog in page code — use the kit Modal or Drawer (app/_kit). (AA-732 kit rule)",
  },
  {
    selector: "JSXAttribute[name.name='className']",
    message:
      "No className on JSX in UI-v2 pages — use inline style + brand tokens (app/_kit/tokens). (AA-732 kit rule)",
  },
];

// Already fully migrated onto the kit AND clean of all three patterns today → enforce at `error`
// so they cannot regress. Measured 2026-10-09 (0 raw <table>, 0 className, 0 hand-rolled dialog).
const KIT_CLEAN_FILES = [
  "app/admin/_components/SettingsKitTab.tsx",
  "app/admin/_components/AdminLiveWriter.tsx",
  "app/(tenant)/portal/_components/ReviewList.tsx",
  "app/(tenant)/portal/_components/CatalogTab.tsx",
];

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  // Override default ignores of eslint-config-next.
  globalIgnores([
    // Default ignores of eslint-config-next:
    ".next/**",
    "out/**",
    "build/**",
    "next-env.d.ts",
  ]),

  // (B) Broad warn: all admin + portal page code. Legacy, not-yet-migrated files surface as
  // warnings — visible, non-blocking, and never rewritten.
  {
    files: ["app/admin/**/*.tsx", "app/(tenant)/portal/**/*.tsx"],
    ignores: ["app/_kit/**"],
    rules: {
      "no-restricted-syntax": ["warn", ...KIT_RESTRICTED_SYNTAX],
    },
  },

  // (A) Specific error: files already on the kit and clean today. Last match wins → `error`.
  {
    files: KIT_CLEAN_FILES,
    rules: {
      "no-restricted-syntax": ["error", ...KIT_RESTRICTED_SYNTAX],
    },
  },
]);

export default eslintConfig;
