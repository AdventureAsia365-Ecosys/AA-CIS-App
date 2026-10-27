// app/_kit/theme.ts
// CIS admin's theme store (AA-601 part B; AA-994 S1a): the kit core store (core/theme.ts) bound to the CIS key
// `cis_theme`. app/layout.tsx puts `themeStore.restoreScript` in its <head>; app/_kit/useTheme.ts binds the hooks.
// No "use client": the server root layout reads the restore script from here.

import { createThemeStore } from "./core/theme";

export type { ResolvedTheme, ThemeChoice } from "./core/theme";

export const THEME_KEY = "cis_theme";

export const themeStore = createThemeStore({ storageKey: THEME_KEY });
