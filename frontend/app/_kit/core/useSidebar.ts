// UI kit core (root ADR 0016). Canonical: AA-CIS-App frontend/app/_kit/core. Edit only there; copies are checked byte for byte (kit.lock.json).
"use client";
// app/_kit/core/useSidebar.ts
// AA-752 — React binding for the sidebar collapsed-state store (app/_kit/sidebarStore.ts).
// useSyncExternalStore, so it is React-Compiler safe (no setState-in-effect) and reads the stored
// value without a flash (same pattern as useThemeChoice).

import { useSyncExternalStore } from "react";
import {
  getCollapsedSnapshot,
  getServerCollapsedSnapshot,
  setCollapsed,
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
