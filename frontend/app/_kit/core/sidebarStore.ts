// UI kit core (root ADR 0016). Canonical: AA-CIS-App frontend/app/_kit/core. Edit only there; copies are checked byte for byte (kit.lock.json).
"use client";
// app/_kit/core/sidebarStore.ts
// AA-752 — admin sidebar collapsed state, shared by the Sidebar toggle, the `[` key handler and
// the AdminShell layout grid (which widens/narrows the sidebar track). Since S264 also the per-group fold state of
// the nav (end of file), used by every app's sidebar through useNavGroups (useSidebar.ts) and NavGroup.tsx.
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

// ── Nav sub-groups (S264) ──────────────────────────────────────────────────────────────────────────────────────────
// Which titled nav groups of an app's sidebar (Pipeline, Assets, CRM, Money…) are folded. One localStorage key per app
// (`aa_nav_groups:<scope>`, scope = "cis-admin", "booking", "crm-web", "portal"…) holding a sorted comma list of
// `<group id>=open|closed`; a group with no entry is folded. The group holding the current page is open whatever is
// stored (resolveNavGroupOpen), so a reload never hides the active item. Same external-store model as above: the
// snapshot is the raw string (stable by value), the server snapshot is "" (every group folded but the active one), and
// with storage blocked the fold lives in memory for the page's lifetime instead of persisting.

export const NAV_GROUPS_KEY_PREFIX = "aa_nav_groups:";

export type NavGroupState = "open" | "closed";

/** The storage key of one app's group fold state. */
export function navGroupsKey(scope: string): string {
  return NAV_GROUPS_KEY_PREFIX + scope;
}

/** A stable group id from its (English) label: "People & access" → "people-access". */
export function navGroupId(label: string): string {
  return label.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "") || "group";
}

/** Parse the stored list; unknown or malformed entries are skipped. */
export function parseNavGroups(raw: string): Record<string, NavGroupState> {
  const out: Record<string, NavGroupState> = {};
  for (const part of raw.split(",")) {
    const eq = part.indexOf("=");
    if (eq <= 0) continue;
    const id = part.slice(0, eq).trim();
    const state = part.slice(eq + 1).trim();
    if (state === "open" || state === "closed") out[id] = state;
  }
  return out;
}

/** The stored form: entries sorted by id, so equal maps give equal strings. */
export function serializeNavGroups(map: Record<string, NavGroupState>): string {
  return Object.keys(map).sort().map((id) => `${id}=${map[id]}`).join(",");
}

/**
 * Is a group shown open? The active group (it holds the current page) is open unless the user folded it on this very
 * page (`dismissed`); any other group follows the stored state, folded when nothing is stored.
 */
export function resolveNavGroupOpen(
  id: string,
  stored: Record<string, NavGroupState>,
  activeId: string | null,
  dismissed = false,
): boolean {
  if (id === activeId) return !dismissed;
  return stored[id] === "open";
}

// Fallback when localStorage throws (blocked site data): the state still toggles, it just does not survive a reload.
const memoryGroups = new Map<string, string>();

export function readNavGroupsRaw(scope: string): string {
  if (typeof window === "undefined") return "";
  try {
    return window.localStorage.getItem(navGroupsKey(scope)) ?? "";
  } catch {
    return memoryGroups.get(scope) ?? "";
  }
}

const groupListeners = new Set<() => void>();

export function subscribeNavGroups(scope: string, cb: () => void): () => void {
  groupListeners.add(cb);
  const key = navGroupsKey(scope);
  const onStorage = (e: StorageEvent) => {
    if (e.key === key) cb();
  };
  if (typeof window !== "undefined") window.addEventListener("storage", onStorage);
  return () => {
    groupListeners.delete(cb);
    if (typeof window !== "undefined") window.removeEventListener("storage", onStorage);
  };
}

/** Store one group's state for `scope` and notify the subscribers. */
export function setNavGroupState(scope: string, id: string, state: NavGroupState): void {
  if (typeof window === "undefined") return;
  const map = parseNavGroups(readNavGroupsRaw(scope));
  map[id] = state;
  const raw = serializeNavGroups(map);
  try {
    window.localStorage.setItem(navGroupsKey(scope), raw);
  } catch {
    memoryGroups.set(scope, raw);
  }
  for (const l of groupListeners) l();
}
