import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

import { STAGED_RULES, kitConfig } from "./app/_kit/core/eslint-kit.mjs";

// AA-994 S1c (root ADR 0016 decision 7) — UI kit conformance rules, shipped with the kit core
// (app/_kit/core/eslint-kit.mjs, byte-identical in every app that vendors the core). The `inline` preset
// (inline style + K tokens) runs at `error` on all page code under app/; the kit itself (app/_kit/**: the
// shell, DataTable, Modal, Drawer, PageFrame) is exempt because it is where the raw elements belong.
// Rules: no raw <main> / <table> / dialog, no hard-coded colour, no hand-styled <button>, no var(--aa-*)
// string, no --aa-* definition from JS, no import of the legacy adminUi / portal ui.tsx layers, no className,
// and (AA-994 S6a, the staged rule CIS turns on first) no raw <input> / <select> / <textarea>: the kit Field + Input,
// Select, Textarea, Checkbox, Radio, DateField.
//
// Existing violations (of these and of the Next rules) are frozen in eslint-suppressions.json (ESLint bulk
// suppressions). The file may only shrink: CI ("Frontend Lint") runs `eslint --prune-suppressions`, fails when
// the committed file differs from the pruned one, and fails when an entry grows against the base branch
// (app/_kit/core/check-kit-copy.mjs --suppressions; a new entry only for a deleted/renamed file's violations).
// After fixing violations: npx eslint --prune-suppressions. Never run --suppress-all to hide a new violation, and
// never switch a kit rule off inline (`eslint-disable… aa-kit/…` or a bare `eslint-disable`): CI fails on it
// (check-kit-copy.mjs --no-kit-disable). Disables that name other rules (react-hooks/…) stay allowed.
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

  kitConfig({
    preset: "inline",
    files: ["app/**/*.{ts,tsx,js,jsx,mjs}"],
    ignores: ["app/_kit/**"],
    rules: STAGED_RULES,
  }),
]);

export default eslintConfig;
