// UI kit core (root ADR 0016). Canonical: AA-CIS-App frontend/app/_kit/core. Edit only there; copies are checked byte for byte (kit.lock.json).
"use client";
// app/_kit/core/useSidebar.ts
// AA-752 — React binding for the sidebar collapsed-state store (app/_kit/sidebarStore.ts).
// useSyncExternalStore, so it is React-Compiler safe (no setState-in-effect) and reads the stored
// value without a flash (same pattern as useThemeChoice).
// S264 — useNavGroups: the per-group fold state of an app's sidebar nav (sidebarStore.ts, end of file).

import { useCallback, useState, useSyncExternalStore } from "react";
import {
  getCollapsedSnapshot,
  getServerCollapsedSnapshot,
  parseNavGroups,
  readNavGroupsRaw,
  resolveNavGroupOpen,
  setCollapsed,
  setNavGroupState,
  subscribeNavGroups,
  subscribeSidebar,
} from "./sidebarStore";

export function useSidebarCollapsed(): [boolean, (c: boolean) => void] {
  const collapsed = useSyncExternalStore(
    subscribeSidebar,
    getCollapsedSnapshot,
    getServerCollapsedSnapshot,
  );
  return [collapsed, setCollapsed];
}

export interface NavGroups {
  /** Is group `id` shown open (stored state, the active group forced open)? */
  isOpen: (id: string) => boolean;
  /** Fold or unfold group `id`; the choice is stored for the app (`scope`). */
  toggle: (id: string) => void;
}

/**
 * The fold state of the titled groups of one app's sidebar nav.
 *
 * `scope` names the app ("cis-admin", "booking", "crm-web", "portal"), `activeId` the group holding the current page
 * (null when none) and `route` the current pathname. The active group is open on every page load and navigation; the
 * user may still fold it, which holds until the route changes (state keyed by `route`, no effect needed). Other
 * groups follow the stored choice and are folded when there is none.
 */
export function useNavGroups(scope: string, activeId: string | null, route: string): NavGroups {
  const subscribe = useCallback((cb: () => void) => subscribeNavGroups(scope, cb), [scope]);
  const getSnapshot = useCallback(() => readNavGroupsRaw(scope), [scope]);
  const raw = useSyncExternalStore(subscribe, getSnapshot, () => "");
  // The active group folded by hand on this route: forgotten as soon as the route (or active group) changes.
  const [dismissed, setDismissed] = useState<{ route: string; id: string } | null>(null);
  const activeDismissed = dismissed != null && dismissed.route === route && dismissed.id === activeId;
  const stored = parseNavGroups(raw);
  const isOpen = (id: string) => resolveNavGroupOpen(id, stored, activeId, activeDismissed);
  const toggle = (id: string) => {
    const next = !isOpen(id);
    setNavGroupState(scope, id, next ? "open" : "closed");
    if (id === activeId) setDismissed(next ? null : { route, id });
  };
  return { isOpen, toggle };
}
