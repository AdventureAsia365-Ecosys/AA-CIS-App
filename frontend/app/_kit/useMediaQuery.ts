// app/_kit/useMediaQuery.ts
// AA-752 — a React-Compiler-safe media-query hook (useSyncExternalStore, no setState-in-effect,
// same pattern as useSidebarCollapsed / useThemeChoice). Server snapshot is always false so SSR
// renders the desktop layout and the client reconciles on mount.

import { useSyncExternalStore } from "react";

// Phone breakpoint shared by the admin shell (globals.css off-canvas drawer at < 768px). Cards
// mode in the kit DataTable and the legacy-table card renderers use this same cut.
export const PHONE_QUERY = "(max-width: 767px)";

function subscribe(query: string): (cb: () => void) => () => void {
  return (cb) => {
    if (typeof window === "undefined" || !window.matchMedia) return () => {};
    const mql = window.matchMedia(query);
    mql.addEventListener("change", cb);
    return () => mql.removeEventListener("change", cb);
  };
}

export function useMediaQuery(query: string): boolean {
  return useSyncExternalStore(
    subscribe(query),
    () => (typeof window !== "undefined" && window.matchMedia ? window.matchMedia(query).matches : false),
    () => false,
  );
}

/** True when the viewport is a phone (< 768px). */
export function useIsPhone(): boolean {
  return useMediaQuery(PHONE_QUERY);
}
