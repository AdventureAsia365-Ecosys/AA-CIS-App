"use client";
// app/_kit/theme.ts
// AA-601 part B — admin theme state (light / dark / system) shared by the AdminSidebar toggle,
// the restore script in layout.tsx, and the chart-colour hook.
//
// Model:
//   - localStorage key `cis_theme` holds the user's CHOICE: 'light' | 'dark' | 'system'
//     (absent === 'system'). The same key is what layout.tsx's inline restore script reads before
//     first paint, so there is no flash of the wrong theme.
//   - The RESOLVED theme ('light' | 'dark') is what actually applies: for 'system' it follows
//     `prefers-color-scheme`, otherwise it is the choice itself. It is written to
//     <html data-theme="…">, which drives the `--aa-*` CSS variables in globals.css.
//
// React Compiler safety (CONTEXT.md / lessons `## Frontend`): no setState-in-effect. Components
// subscribe through useSyncExternalStore; the only mutations are explicit event handlers
// (setThemeChoice) and the matchMedia/storage listeners the store itself owns.

export type ThemeChoice = "light" | "dark" | "system";
export type ResolvedTheme = "light" | "dark";

export const THEME_KEY = "cis_theme";

function systemPrefersDark(): boolean {
  return typeof window !== "undefined" && window.matchMedia("(prefers-color-scheme: dark)").matches;
}

export function readChoice(): ThemeChoice {
  if (typeof window === "undefined") return "system";
  const v = window.localStorage.getItem(THEME_KEY);
  return v === "light" || v === "dark" ? v : "system";
}

export function resolveTheme(choice: ThemeChoice): ResolvedTheme {
  if (choice === "system") return systemPrefersDark() ? "dark" : "light";
  return choice;
}

/** Apply the resolved theme to <html data-theme>. Safe to call repeatedly. */
export function applyResolvedTheme(choice: ThemeChoice): void {
  if (typeof document === "undefined") return;
  document.documentElement.setAttribute("data-theme", resolveTheme(choice));
}

// ── External store (choice) ────────────────────────────────────────────────
const listeners = new Set<() => void>();

function emit(): void {
  for (const l of listeners) l();
}

export function subscribeThemeChoice(cb: () => void): () => void {
  listeners.add(cb);
  // Follow OS changes while on "system", and cross-tab localStorage changes.
  const mq = typeof window !== "undefined" ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  const onMedia = () => {
    if (readChoice() === "system") {
      applyResolvedTheme("system");
      emit();
    }
  };
  const onStorage = (e: StorageEvent) => {
    if (e.key === THEME_KEY) {
      applyResolvedTheme(readChoice());
      emit();
    }
  };
  mq?.addEventListener("change", onMedia);
  if (typeof window !== "undefined") window.addEventListener("storage", onStorage);
  return () => {
    listeners.delete(cb);
    mq?.removeEventListener("change", onMedia);
    if (typeof window !== "undefined") window.removeEventListener("storage", onStorage);
  };
}

export function getThemeChoiceSnapshot(): ThemeChoice {
  return readChoice();
}

export function getServerThemeChoiceSnapshot(): ThemeChoice {
  return "system";
}

/** Persist a new choice and apply it immediately, notifying subscribers. */
export function setThemeChoice(choice: ThemeChoice): void {
  if (typeof window === "undefined") return;
  if (choice === "system") window.localStorage.removeItem(THEME_KEY);
  else window.localStorage.setItem(THEME_KEY, choice);
  applyResolvedTheme(choice);
  emit();
}
