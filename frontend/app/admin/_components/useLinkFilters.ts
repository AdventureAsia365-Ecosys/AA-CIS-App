"use client";
// app/admin/_components/useLinkFilters.ts — AA-1156: list filters a link may open the page with (?status=…, the My
// work tiles). The URL is read once hydrated (useSyncExternalStore: the server render and the first client render both
// use `empty`, so they agree; the Place cards pattern), and the user's own choice wins from then on. No setState in an
// effect (React Compiler). Used by Supply copy and AA content.

import { useState, useSyncExternalStore } from "react";

const noopSubscribe = () => () => {};

/** [filters, setFilters]: the URL's filters (parsed by `fromSearch`) until the user picks one. */
export function useLinkFilters<F>(fromSearch: (search: string) => F, empty: F): [F, (next: F) => void] {
  const hydrated = useSyncExternalStore(noopSubscribe, () => true, () => false);
  const [edited, setEdited] = useState<F | null>(null);
  const filters = edited ?? (hydrated ? fromSearch(window.location.search) : empty);
  return [filters, setEdited];
}
