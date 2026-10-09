"use client";
// app/_kit/sidebarStore.ts
// AA-752 — admin sidebar collapsed state, shared by the Sidebar toggle, the `[` key handler and
// the AdminShell layout grid (which widens/narrows the sidebar track).
//
// Model (mirrors theme.ts): localStorage key `cis_sidebar` holds "collapsed" | "expanded"
// (absent === expanded). The inline restore script in app/layout.tsx reads it before first paint
// (sets data-sidebar on <html>) so there is no flash of the wrong width. Components subscribe
// through useSyncExternalStore — React-Compiler safe (no setState-in-effect); the only mutations
// are the explicit toggle handler and the cross-tab storage listener the store owns.

export const SIDEBAR_KEY = "cis_sidebar";

export function readCollapsed(): boolean {
  if (typeof window === "undefined") return false;
  return window.localStorage.getItem(SIDEBAR_KEY) === "collapsed";
}

/** Reflect the collapsed state on <html data-sidebar> so CSS / the restore script agree. */
function applyAttr(collapsed: boolean): void {
  if (typeof document === "undefined") return;
  document.documentElement.setAttribute("data-sidebar", collapsed ? "collapsed" : "expanded");
}

const listeners = new Set<() => void>();

function emit(): void {
  for (const l of listeners) l();
}

export function subscribeSidebar(cb: () => void): () => void {
  listeners.add(cb);
  const onStorage = (e: StorageEvent) => {
    if (e.key === SIDEBAR_KEY) {
      applyAttr(readCollapsed());
      emit();
    }
  };
  if (typeof window !== "undefined") window.addEventListener("storage", onStorage);
  return () => {
    listeners.delete(cb);
    if (typeof window !== "undefined") window.removeEventListener("storage", onStorage);
  };
}

export function getCollapsedSnapshot(): boolean {
  return readCollapsed();
}

export function getServerCollapsedSnapshot(): boolean {
  return false;
}

export function setCollapsed(collapsed: boolean): void {
  if (typeof window === "undefined") return;
  if (collapsed) window.localStorage.setItem(SIDEBAR_KEY, "collapsed");
  else window.localStorage.removeItem(SIDEBAR_KEY);
  applyAttr(collapsed);
  emit();
}
