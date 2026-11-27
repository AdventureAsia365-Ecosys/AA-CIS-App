"use client";
// app/admin/_components/useLinkFilters.ts — AA-1156: list filters a link may open the page with (?status=…, the My
// work tiles). Used by Supply copy, AA content, Place cards, the Review Queue and Data erasure (AA-1190).
//
// The URL is read with useSearchParams(), never window.location: on a client-side <Link> navigation the new page
// renders BEFORE the browser URL changes, so a window.location read during render sees the previous page's query and
// nothing re-reads it (S286 live check: Place cards opened from My work listed every card, 4,860, under
// ?status=auto_checked; S296: the Review Queue and Data erasure tiles opened unfiltered). useSearchParams follows the
// router's URL, on the first render of a client navigation and on a direct load alike. It needs a <Suspense>
// boundary above the page body for `next build` (the page.tsx files).
//
// The user's own choice wins until the URL changes (AA-1190): a choice remembers the query it was made on, and a new
// query (Back / Forward, a link to the same page with another filter, the menu link without one) shows what that URL
// asks for again. A page that writes its view into the URL (history.replaceState) stays consistent: Next syncs the
// router with it, the query changes and `fromSearch` of the written query is the same view. No setState in an effect
// (React Compiler).

import { useSearchParams } from "next/navigation";
import { useState } from "react";

/** [filters, setFilters]: the URL's filters (parsed by `fromSearch`), or the user's pick made on this same URL. */
export function useLinkFilters<F>(fromSearch: (search: string) => F): [F, (next: F) => void] {
  const search = useSearchParams().toString();
  const [edited, setEdited] = useState<{ search: string; value: F } | null>(null);
  const filters = edited !== null && edited.search === search ? edited.value : fromSearch(search);
  return [filters, (next: F) => setEdited({ search, value: next })];
}
