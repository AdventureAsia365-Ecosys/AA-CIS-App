"use client";
// app/_kit/useNavSections.ts
// AA-883 — which sidebar sections (Platform / CIS Content) the user folded. Same model as
// sidebarStore.ts: localStorage key `cis_nav_closed` holds a comma list of closed section ids
// (absent === every section open), read through useSyncExternalStore (React-Compiler safe, no
// setState-in-effect). The server snapshot is "all open", so SSR and the first client render agree.

import { useSyncExternalStore } from "react";
import type { NavSectionId } from "./adminNav";

export const NAV_CLOSED_KEY = "cis_nav_closed";

const listeners = new Set<() => void>();

function readRaw(): string {
  if (typeof window === "undefined") return "";
  try {
    return window.localStorage.getItem(NAV_CLOSED_KEY) ?? "";
  } catch {
    return "";
  }
}

function subscribe(cb: () => void): () => void {
  listeners.add(cb);
  const onStorage = (e: StorageEvent) => {
    if (e.key === NAV_CLOSED_KEY) cb();
  };
  if (typeof window !== "undefined") window.addEventListener("storage", onStorage);
  return () => {
    listeners.delete(cb);
    if (typeof window !== "undefined") window.removeEventListener("storage", onStorage);
  };
}

function write(closed: Set<string>): void {
  if (typeof window === "undefined") return;
  try {
    if (closed.size) window.localStorage.setItem(NAV_CLOSED_KEY, [...closed].sort().join(","));
    else window.localStorage.removeItem(NAV_CLOSED_KEY);
  } catch {
    /* storage blocked: the fold just does not persist */
  }
  for (const l of listeners) l();
}

/** [isClosed(section), toggle(section)]. The snapshot is the raw string, so it is stable by value. */
export function useNavSections(): [(id: NavSectionId) => boolean, (id: NavSectionId) => void] {
  const raw = useSyncExternalStore(subscribe, readRaw, () => "");
  const closed = new Set(raw.split(",").filter(Boolean));
  const isClosed = (id: NavSectionId) => closed.has(id);
  const toggle = (id: NavSectionId) => {
    const next = new Set(closed);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    write(next);
  };
  return [isClosed, toggle];
}
