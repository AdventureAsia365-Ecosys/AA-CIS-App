// UI kit core (root ADR 0016). Canonical: AA-CIS-App frontend/app/_kit/core. Edit only there; copies are checked byte for byte (kit.lock.json).
// app/_kit/core/theme.ts
// AA-601 part B, made portable in AA-994 S1a (root ADR 0016 decision 4) — theme state (light / dark / system) for
// the theme toggle, the restore script in the app's layout.tsx, and the chart-colour hook.
//
// Each app makes ONE store with its own localStorage key (`cis_theme` in CIS admin, `bk_theme` in Booking,
// `tp_theme` in TripPlanner; localStorage is per origin, so the keys never collide):
//   export const themeStore = createThemeStore({ storageKey: "cis_theme" });
// and puts `themeStore.restoreScript` in an inline <script> in the <head> of its root layout.
//
// Model:
//   - the storage key holds the user's CHOICE: 'light' | 'dark' | 'system' (absent === 'system'). The restore
//     script reads the same key before first paint, so there is no flash of the wrong theme.
//   - The RESOLVED theme ('light' | 'dark') is what actually applies: for 'system' it follows
//     `prefers-color-scheme`, otherwise it is the choice itself. It is written to
//     <html data-theme="…">, which drives the `--aa-*` CSS variables.
//
// No "use client": the module has no React and its functions guard `window` / `document`, so a server layout can
// read `restoreScript`. React Compiler safety (lessons `## Frontend`): no setState-in-effect. Components subscribe
// through useSyncExternalStore (core/useTheme.ts); the only mutations are explicit event handlers (setChoice) and
// the matchMedia/storage listeners the store itself owns.

export type ThemeChoice = "light" | "dark" | "system";
export type ResolvedTheme = "light" | "dark";

export interface ThemeStore {
  /** The localStorage key that holds the choice. */
  readonly storageKey: string;
  readChoice(): ThemeChoice;
  resolveTheme(choice: ThemeChoice): ResolvedTheme;
  /** Apply the resolved theme to <html data-theme>. Safe to call repeatedly. */
  applyResolvedTheme(choice: ThemeChoice): void;
  /** useSyncExternalStore subscribe: OS changes while on "system", and cross-tab changes of the key. */
  subscribe(cb: () => void): () => void;
  getSnapshot(): ThemeChoice;
  getServerSnapshot(): ThemeChoice;
  /** Persist a new choice and apply it immediately, notifying subscribers. */
  setChoice(choice: ThemeChoice): void;
  /** The inline script for the root layout's <head>: applies the stored choice before first paint. */
  readonly restoreScript: string;
}

const KEY_RE = /^[A-Za-z0-9_:.-]{1,64}$/;

function checkKey(storageKey: string): string {
  if (!KEY_RE.test(storageKey)) {
    throw new Error(`theme storage key must match ${KEY_RE}: ${JSON.stringify(storageKey)}`);
  }
  return storageKey;
}

function systemPrefersDark(): boolean {
  return typeof window !== "undefined" && window.matchMedia("(prefers-color-scheme: dark)").matches;
}

/** The restore script for a key (also on every store as `restoreScript`). */
export function themeRestoreScript(storageKey: string): string {
  const key = JSON.stringify(checkKey(storageKey));
  return `
          (function() {
            try {
              var c = localStorage.getItem(${key});
              var resolved = (c === 'light' || c === 'dark')
                ? c
                : (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
              document.documentElement.setAttribute('data-theme', resolved);
            } catch (e) {
              document.documentElement.setAttribute('data-theme', 'light');
            }
          })();
        `;
}

export function createThemeStore({ storageKey }: { storageKey: string }): ThemeStore {
  const key = checkKey(storageKey);
  const listeners = new Set<() => void>();

  function emit(): void {
    for (const l of listeners) l();
  }

  function readChoice(): ThemeChoice {
    if (typeof window === "undefined") return "system";
    const v = window.localStorage.getItem(key);
    return v === "light" || v === "dark" ? v : "system";
  }

  function resolveTheme(choice: ThemeChoice): ResolvedTheme {
    if (choice === "system") return systemPrefersDark() ? "dark" : "light";
    return choice;
  }

  function applyResolvedTheme(choice: ThemeChoice): void {
    if (typeof document === "undefined") return;
    document.documentElement.setAttribute("data-theme", resolveTheme(choice));
  }

  function subscribe(cb: () => void): () => void {
    listeners.add(cb);
    const mq = typeof window !== "undefined" ? window.matchMedia("(prefers-color-scheme: dark)") : null;
    const onMedia = () => {
      if (readChoice() === "system") {
        applyResolvedTheme("system");
        emit();
      }
    };
    const onStorage = (e: StorageEvent) => {
      if (e.key === key) {
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

  function setChoice(choice: ThemeChoice): void {
    if (typeof window === "undefined") return;
    if (choice === "system") window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, choice);
    applyResolvedTheme(choice);
    emit();
  }

  return {
    storageKey: key,
    readChoice,
    resolveTheme,
    applyResolvedTheme,
    subscribe,
    getSnapshot: readChoice,
    getServerSnapshot: () => "system",
    setChoice,
    restoreScript: themeRestoreScript(key),
  };
}
