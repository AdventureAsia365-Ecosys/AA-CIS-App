// frontend/lib/placeCardsView.test.mjs
// AA-1156 (S286 live check F1): a My work tile opens its list filtered after a client-side click, not only on a
// direct load. Checks:
//   1. placeCardsViewFromSearch / placeCardsSearch round-trip the Place cards view (status, search, 1-based page);
//   2. the shared link-filter hook reads the URL with useSearchParams, never window.location (on a <Link> navigation
//      the new page renders before the browser URL changes, so a window.location read sees the old query);
//   3. no My work target list reads window.location.search, and every page that uses the hook has a Suspense
//      boundary (useSearchParams without one fails `next build`);
//   4. AA-1190 (S296 live check: the Review Queue and Data erasure tiles opened unfiltered after a click): the page
//      folder of EVERY My work tile (lib/myWork.ts MY_WORK_TILES) reads no window.location.search, bar the listed
//      URL writes in event handlers. The click itself is tested in tests/e2e/admin-routes/my-work-tile-links.spec.ts.
// Run: npm run test:unit.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { readdirSync } from "node:fs";

import { MY_WORK_TILES } from "./myWork.ts";
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
  // AA-1190: a pick holds only on the URL it was made on (Back / Forward and a new link show the URL's filters)
  assert.match(hook, /edited\.search === search/);
});

// The list components of the My work tile targets that read a filter from the URL, and the page that renders each.
const LINK_FILTER_LISTS = [
  ["app/admin/places/cards/page.tsx", "app/admin/places/cards/page.tsx"],
  ["app/admin/supply-content/SupplyContentList.tsx", "app/admin/supply-content/page.tsx"],
  ["app/admin/aa-content/AaContentConsole.tsx", "app/admin/aa-content/page.tsx"],
  ["app/admin/tenants/data-erasure/DataErasure.tsx", "app/admin/tenants/data-erasure/page.tsx"],
];

test("3. the tile target lists read the URL through the hook, under a Suspense boundary", () => {
  for (const [list, page] of LINK_FILTER_LISTS) {
    const src = code(list);
    assert.match(src, /useLinkFilters\(/, list);
    assert.doesNotMatch(src, /window\.location\.search/, `${list} reads window.location.search`);
    assert.match(read(page), /<Suspense>/, `${page} has no Suspense boundary`);
  }
});

// Files of a tile's page folder that may name window.location.search: a URL WRITE in an event handler (it runs after
// the navigation, so the URL is current), never a read during render. Keep this list short and say why.
const URL_WRITES_ONLY = new Set([
  "app/admin/overview/page.tsx", // setView / reset write the next view query; the view is read with useSearchParams
]);

test("4. no My work tile target reads the query from window.location", () => {
  const dirs = [...new Set(MY_WORK_TILES.map((t) => `app${t.path}`))];
  assert.ok(dirs.length >= 8, "the tile list is empty");
  for (const dir of dirs) {
    const files = readdirSync(join(FRONTEND, dir)).filter((f) => /\.tsx?$/.test(f) && !f.endsWith(".messages.ts"));
    assert.ok(files.includes("page.tsx"), `${dir} has no page.tsx`);
    for (const f of files) {
      const rel = `${dir}/${f}`;
      if (URL_WRITES_ONLY.has(rel)) continue;
      assert.doesNotMatch(code(rel), /window\.location\.search/, `${rel} reads window.location.search`);
    }
  }
  // the Review Queue reads its link with useSearchParams under a Suspense boundary
  assert.match(code("app/admin/review/page.tsx"), /useSearchParams\(\)/);
  assert.match(read("app/admin/review/page.tsx"), /<Suspense>/);
});
