// lib/placeCardsView.ts — AA-1156 (S286 live check F1): the AA Catalogue › Place cards list view (status filter,
// place search, page) as a query string, both ways. The page reads it through useLinkFilters (useSearchParams), so
// the My work tile "Place cards to review" (?status=auto_checked) opens the list filtered after a client-side click,
// not only on a direct load. Pure, so node --test checks it (lib/placeCardsView.test.mjs).

export interface PlaceCardsView { status: string; q: string; page: number }

/** The view a query string asks for (`?status=&q=&page=`, page 1-based in the URL, 0-based here). */
export function placeCardsViewFromSearch(search: string): PlaceCardsView {
  const p = new URLSearchParams(search);
  return { status: p.get("status") ?? "", q: p.get("q") ?? "", page: Math.max(0, (Number(p.get("page")) || 1) - 1) };
}

/** The query string of a view, without "?" (empty for the default view). */
export function placeCardsSearch(v: PlaceCardsView): string {
  const p = new URLSearchParams();
  if (v.status) p.set("status", v.status);
  if (v.q) p.set("q", v.q);
  if (v.page) p.set("page", String(v.page + 1));
  return p.toString();
}
