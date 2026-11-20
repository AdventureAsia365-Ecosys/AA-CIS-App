"use client";
// app/admin/_components/useLinkFilters.ts — AA-1156: list filters a link may open the page with (?status=…, the My
// work tiles). Used by Supply copy, AA content and Place cards.
//
// The URL is read with useSearchParams(), never window.location: on a client-side <Link> navigation the new page
// renders BEFORE the browser URL changes, so a window.location read during render sees the previous page's query and
// nothing re-reads it (S286 live check: Place cards opened from My work listed every card, 4,860, under
// ?status=auto_checked). useSearchParams follows the router's URL, on the first render of a client navigation and on
// a direct load alike. It needs a <Suspense> boundary above the page body for `next build` (the page.tsx files).
// The user's own choice wins from then on. No setState in an effect (React Compiler).

import { useSearchParams } from "next/navigation";
import { useState } from "react";

/** [filters, setFilters]: the URL's filters (parsed by `fromSearch`) until the user picks one. */
export function useLinkFilters<F>(fromSearch: (search: string) => F): [F, (next: F) => void] {
  const search = useSearchParams().toString();
  const [edited, setEdited] = useState<F | null>(null);
  const filters = edited ?? fromSearch(search);
  return [filters, setEdited];
}
