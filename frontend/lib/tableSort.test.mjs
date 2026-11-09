// frontend/lib/tableSort.test.mjs
// AA-1006 (review fix): a page-owned sort on the kit DataTable and the phone card title order.
// Run: npm run test:unit (node --test with --experimental-strip-types).
//
// The header-click cases drive a real TanStack table (the engine under the kit DataTable): a column needs an
// accessor to be sortable at all, and a click goes through `getToggleSortingHandler()` exactly as the kit header does.

import assert from "node:assert/strict";
import { test } from "node:test";

import { createTable, getCoreRowModel } from "@tanstack/table-core";

import { clickedSortKey, nextPageSort, pageSortProps, phoneCardColumns } from "./tableSort.ts";

const ROWS = [
  { name: "Lantern Valley Walk", country: "Testland", score: 8.4 },
  { name: "Copper Bay Cruise", country: null, score: null },
];

/** A table like the kit DataTable builds it (controlled sorting, manualSorting, multi-sort on). */
function tableFor(columns, sortProps) {
  const state = { sorting: sortProps.sorting };
  return createTable({
    data: ROWS,
    columns,
    state,
    onStateChange: () => {},
    renderFallbackValue: null,
    getCoreRowModel: getCoreRowModel(),
    enableMultiSort: true,
    manualSorting: sortProps.manualSorting,
    onSortingChange: (updater) => {
      const next = typeof updater === "function" ? updater(state.sorting) : updater;
      sortProps.onSortingChange(next);
    },
  });
}

const firstDir = (k) => (k === "score" ? "desc" : "asc");

test("a column with only id + cell cannot sort; with an accessorFn it can", () => {
  const sortProps = pageSortProps({ key: "name", dir: "asc" }, () => {}, firstDir);
  const t = tableFor([
    { id: "name", cell: () => null },
    { id: "country", accessorFn: (r) => r.country ?? "", cell: () => null },
    { id: "actions", enableSorting: false, accessorFn: (r) => r.name, cell: () => null },
  ], sortProps);
  assert.equal(t.getColumn("name").getCanSort(), false, "no accessor: the header click is never wired");
  assert.equal(t.getColumn("country").getCanSort(), true);
  assert.equal(t.getColumn("actions").getCanSort(), false);
});

test("a header click through TanStack applies the page's rule: same column flips, a new one starts at firstDir", () => {
  const cols = [
    { id: "name", accessorFn: (r) => r.name, cell: () => null },
    { id: "country", accessorFn: (r) => r.country ?? "", cell: () => null },
    { id: "score", accessorFn: (r) => r.score ?? -1, cell: () => null },
  ];
  const click = (sort, id) => {
    let got = null;
    const t = tableFor(cols, pageSortProps(sort, (s) => { got = s; }, firstDir));
    t.getColumn(id).getToggleSortingHandler()({});
    return got;
  };
  // The sorted column flips both ways (TanStack's own "remove the sort" step included).
  assert.deepEqual(click({ key: "name", dir: "asc" }, "name"), { key: "name", dir: "desc" });
  assert.deepEqual(click({ key: "name", dir: "desc" }, "name"), { key: "name", dir: "asc" });
  assert.deepEqual(click({ key: "score", dir: "desc" }, "score"), { key: "score", dir: "asc" });
  assert.deepEqual(click({ key: "score", dir: "asc" }, "score"), { key: "score", dir: "desc" });
  // Another column starts at its own first direction.
  assert.deepEqual(click({ key: "name", dir: "asc" }, "score"), { key: "score", dir: "desc" });
  assert.deepEqual(click({ key: "score", dir: "desc" }, "country"), { key: "country", dir: "asc" });
  // The page's sort key need not be a column (Master Content sorts by created_at by default).
  assert.deepEqual(click({ key: "created_at", dir: "desc" }, "name"), { key: "name", dir: "asc" });
});

test("a shift-click (multi-sort) still names the clicked column", () => {
  assert.equal(clickedSortKey([{ id: "name" }, { id: "score" }], "name"), "score");
  assert.equal(clickedSortKey([], "name"), "name");
  assert.deepEqual(nextPageSort([{ id: "name", desc: false }, { id: "score", desc: true }], { key: "name", dir: "asc" },
    () => "desc"), { key: "score", dir: "desc" });
});

test("phoneCardColumns moves the title column first on a phone only", () => {
  const cols = [{ id: "select" }, { id: "n" }, { id: "tour_name" }, { id: "country" }];
  assert.equal(phoneCardColumns(cols, "tour_name", false), cols);
  assert.deepEqual(phoneCardColumns(cols, "tour_name", true).map((c) => c.id), ["tour_name", "select", "n", "country"]);
  assert.deepEqual(phoneCardColumns(cols, "select", true).map((c) => c.id), ["select", "n", "tour_name", "country"]);
  assert.equal(phoneCardColumns(cols, "missing", true), cols);
});
