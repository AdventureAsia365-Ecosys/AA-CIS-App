import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

// AA-885 — the portal app's ESLint config. The UI-kit rules are the same as the admin's
// (frontend/eslint.config.mjs, AA-732): no raw <table>, no hand-rolled role="dialog", no className
// on JSX. Legacy portal files warn; files already on the kit are held to `error`. The vendored kit
// (app/_kit/**) is exempt, exactly as in the admin app.
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

// Already on the kit and clean of all three patterns (moved from the admin list, AA-885).
// AA-759 — the kit rule now holds the whole portal shell (app/portal/_shell) and every route page
// to `error`; the remaining legacy _components stay `warn` until AA-760 migrates them. The shell's
// responsive CSS hooks are `data-pshell` attributes (globals.css), not classNames.
const KIT_CLEAN_FILES = [
  "app/portal/_shell/**/*.tsx",
  "app/portal/**/page.tsx",
  "app/portal/layout.tsx",
  "app/portal/_components/ReviewList.tsx",
  "app/portal/_components/CatalogTab.tsx",
  "app/portal/_components/NotificationsBell.tsx",
];

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  globalIgnores([
    ".next/**",
    "out/**",
    "build/**",
    "next-env.d.ts",
  ]),

  {
    files: ["app/portal/**/*.tsx"],
    ignores: ["app/_kit/**"],
    rules: {
      "no-restricted-syntax": ["warn", ...KIT_RESTRICTED_SYNTAX],
    },
  },

  {
    files: KIT_CLEAN_FILES,
    rules: {
      "no-restricted-syntax": ["error", ...KIT_RESTRICTED_SYNTAX],
    },
  },
]);

export default eslintConfig;
