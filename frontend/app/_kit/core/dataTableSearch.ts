// UI kit core (root ADR 0016). Canonical: AA-CIS-App frontend/app/_kit/core. Edit only there; copies are checked byte for byte (kit.lock.json).
// app/_kit/core/dataTableSearch.ts — S280 (AA-1096): the DataTable search box beyond the visible cells.
//
// Staff search a table by what they know, not only by the display name a cell shows: a tenant slug, a request id, a
// staff e-mail. A page passes `searchText(row)` (the extra text of a row) and the box matches it as well as every
// column's text or number value (TanStack's default skips display-only columns, so with `searchText` every column
// takes part). Without `searchText` the table keeps TanStack's default search. The box also gets an accessible name
// (`searchLabel`, default "Search this table"): a placeholder is not a name. Pure.

import type { FilterFn, Row } from "@tanstack/react-table";

/** One row and column match: the row's extra text or the column's text/number value contains the query
 *  (case-insensitive, trimmed); an empty query matches. */
export function dataTableSearchMatch(extra: string | null | undefined, cellValue: unknown, query: unknown): boolean {
  const q = String(query ?? "").trim().toLowerCase();
  if (!q) return true;
  if ((extra ?? "").toLowerCase().includes(q)) return true;
  return (typeof cellValue === "string" || typeof cellValue === "number") && String(cellValue).toLowerCase().includes(q);
}

/** The useReactTable options for a page's `searchText` (none without it: the default search is unchanged). */
export function searchOptions<T>(searchText: ((row: T) => string | null | undefined) | undefined): {
  getColumnCanGlobalFilter?: () => boolean;
  globalFilterFn?: FilterFn<T>;
} {
  if (!searchText) return {};
  return {
    getColumnCanGlobalFilter: () => true,
    globalFilterFn: (row: Row<T>, columnId: string, value: unknown) =>
      dataTableSearchMatch(searchText(row.original), row.getValue(columnId), value),
  };
}

/** The search box's placeholder and accessible name: the page's, else the kit's words. */
export function searchBoxText(t: (key: "search" | "searchTable") => string, placeholder?: string, label?: string): {
  placeholder: string;
  label: string;
} {
  return { placeholder: placeholder ?? t("search"), label: label ?? t("searchTable") };
}
