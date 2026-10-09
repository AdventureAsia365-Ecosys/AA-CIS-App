"use client";
// app/_kit/useTheme.ts
// AA-601 part B — React bindings for the theme store (app/_kit/theme.ts).
//
// `useThemeChoice()` returns the current choice + a setter (used by the AdminSidebar toggle).
// `useResolvedTheme()` returns 'light' | 'dark' and re-renders on any theme change (OS, toggle,
// cross-tab) via useSyncExternalStore — React-Compiler safe (no setState-in-effect).
//
// Recharts note: `stroke`/`fill` are SVG PRESENTATION ATTRIBUTES, which do NOT resolve CSS
// `var(--…)` (only the CSS fill/stroke PROPERTIES do, and recharts emits attributes). So charts
// cannot pass `var(--aa-…)` straight to `stroke`/`fill`. `useChartColors()` resolves the needed
// variables to concrete hex/rgb with getComputedStyle and recomputes whenever the theme changes.

import { useSyncExternalStore } from "react";
import {
  getServerThemeChoiceSnapshot,
  getThemeChoiceSnapshot,
  resolveTheme,
  setThemeChoice,
  subscribeThemeChoice,
  type ResolvedTheme,
  type ThemeChoice,
} from "./theme";

export function useThemeChoice(): [ThemeChoice, (c: ThemeChoice) => void] {
  const choice = useSyncExternalStore(
    subscribeThemeChoice,
    getThemeChoiceSnapshot,
    getServerThemeChoiceSnapshot,
  );
  return [choice, setThemeChoice];
}

export function useResolvedTheme(): ResolvedTheme {
  const choice = useSyncExternalStore(
    subscribeThemeChoice,
    getThemeChoiceSnapshot,
    getServerThemeChoiceSnapshot,
  );
  return resolveTheme(choice);
}

/**
 * Resolve a set of `--aa-…` CSS variable names to concrete colour strings for the CURRENT theme,
 * for use as SVG stroke/fill attributes in Recharts. Pass a map of friendly-name → css-var-name.
 * Recomputes on every theme change (the resolved theme is a dependency of the memo via
 * useSyncExternalStore). Returns the friendly-name → resolved-colour map.
 *
 * SSR: returns the raw `var(--…)` strings (no document); the first client render recomputes.
 */
export function useChartColors<T extends Record<string, string>>(vars: T): Record<keyof T, string> {
  // Re-subscribe to theme so this recomputes when the theme flips.
  useResolvedTheme();
  const out = {} as Record<keyof T, string>;
  if (typeof document === "undefined") {
    for (const k in vars) out[k] = vars[k];
    return out;
  }
  const cs = getComputedStyle(document.documentElement);
  for (const k in vars) {
    const name = vars[k].replace(/^var\(|\)$/g, "").trim(); // accept "var(--x)" or "--x"
    const val = cs.getPropertyValue(name).trim();
    out[k] = val || vars[k];
  }
  return out;
}
