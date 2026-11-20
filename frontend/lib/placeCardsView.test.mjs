// frontend/lib/placeCardsView.test.mjs
// AA-1156 (S286 live check F1): a My work tile opens its list filtered after a client-side click, not only on a
// direct load. Checks:
//   1. placeCardsViewFromSearch / placeCardsSearch round-trip the Place cards view (status, search, 1-based page);
//   2. the shared link-filter hook reads the URL with useSearchParams, never window.location (on a <Link> navigation
//      the new page renders before the browser URL changes, so a window.location read sees the old query);
//   3. no My work target list reads window.location.search, and every page that uses the hook has a Suspense
//      boundary (useSearchParams without one fails `next build`).
// Run: npm run test:unit.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { placeCardsSearch, placeCardsViewFromSearch } from "./placeCardsView.ts";

const FRONTEND = fileURLToPath(new URL("..", import.meta.url));
const read = (rel) => readFileSync(join(FRONTEND, rel), "utf8");
/** The source without its // line comments (a comment may name what the code must not do). */
const code = (rel) => read(rel).replace(/^\s*\/\/.*$/gm, "");

test("1. the Place cards view and its query string", () => {
  assert.deepEqual(placeCardsViewFromSearch("?status=auto_checked"), { status: "auto_checked", q: "", page: 0 });
  assert.deepEqual(placeCardsViewFromSearch("status=verified&q=Lakeview%20Hill&page=3"),
    { status: "verified", q: "Lakeview Hill", page: 2 });
  assert.deepEqual(placeCardsViewFromSearch(""), { status: "", q: "", page: 0 });
  assert.equal(placeCardsViewFromSearch("?page=0").page, 0);
  assert.equal(placeCardsViewFromSearch("?page=-4").page, 0);
  assert.equal(placeCardsViewFromSearch("?page=abc").page, 0);
  assert.equal(placeCardsSearch({ status: "", q: "", page: 0 }), "");
  const v = { status: "rejected", q: "Old Mill", page: 4 };
  assert.deepEqual(placeCardsViewFromSearch(placeCardsSearch(v)), v);
});

test("2. the link-filter hook follows the router URL", () => {
  const hook = code("app/admin/_components/useLinkFilters.ts");
  assert.match(hook, /useSearchParams\(\)/);
  assert.doesNotMatch(hook, /window\.location/);
});

// The list components of the My work tile targets that read a filter from the URL, and the page that renders each.
const LINK_FILTER_LISTS = [
  ["app/admin/places/cards/page.tsx", "app/admin/places/cards/page.tsx"],
  ["app/admin/supply-content/SupplyContentList.tsx", "app/admin/supply-content/page.tsx"],
  ["app/admin/aa-content/AaContentConsole.tsx", "app/admin/aa-content/page.tsx"],
];

test("3. the tile target lists read the URL through the hook, under a Suspense boundary", () => {
  for (const [list, page] of LINK_FILTER_LISTS) {
    const src = code(list);
    assert.match(src, /useLinkFilters\(/, list);
    assert.doesNotMatch(src, /window\.location\.search/, `${list} reads window.location.search`);
    assert.match(read(page), /<Suspense>/, `${page} has no Suspense boundary`);
  }
});
