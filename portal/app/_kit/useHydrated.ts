"use client";
// app/_kit/useHydrated.ts
// AA-834 — true once this client tree has hydrated, false on the server and during the hydrating
// render. useSyncExternalStore (no setState-in-effect, React-Compiler safe): React renders the
// server snapshot while hydrating, then re-renders with the client snapshot, so `true` only shows
// up in a commit where event handlers are attached. Used as a test hook (`data-hydrated`) so e2e
// specs click only after the shell is interactive.

import { useSyncExternalStore } from "react";

const noopSubscribe = () => () => {};

export function useHydrated(): boolean {
  return useSyncExternalStore(noopSubscribe, () => true, () => false);
}
