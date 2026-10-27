"use client";
// app/_kit/useTheme.ts
// CIS admin's theme hooks (AA-601 part B; AA-994 S1a): the kit core hooks (core/useTheme.ts) bound to the CIS
// theme store (app/_kit/theme.ts, key `cis_theme`). Import path unchanged for the Topbar toggle, the login page and
// the chart pages.

import { createThemeHooks } from "./core/useTheme";
import { themeStore } from "./theme";

export const { useThemeChoice, useResolvedTheme, useChartColors } = createThemeHooks(themeStore);
