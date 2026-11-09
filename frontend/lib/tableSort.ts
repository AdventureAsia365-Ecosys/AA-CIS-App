// lib/tableSort.ts — a page-owned sort and phone card order on the kit DataTable (AA-1006).
//
// Three traps of the kit DataTable (TanStack table) that a converted page must avoid:
//
// 1. A header is clickable only when TanStack says the column can sort, and `column.getCanSort()` needs an accessor
//    (`accessorKey` / `accessorFn`) as well as `enableSorting`. A column with only `id` + `cell` never sorts, so a page
//    that keeps its own sort (server side, or its own comparator) must still give each sortable column an
//    `accessorFn` (any field read: with `manualSorting` the value only enables the header).
// 2. The page decides the direction, as the old header buttons did: a click on the sorted column flips it, a click on
//    another column starts at that column's first direction. TanStack proposes its own next state (asc → desc →
//    removed), so `nextPageSort()` reads only which column was clicked and applies the page's rule.
// 3. On a phone the kit table draws a card per row whose title is the first column. A page whose first column is a
//    checkbox or a row number moves its name column first on a phone only (`phoneCardColumns()`), so the desktop
//    column order stays as it was.

export type SortDir = "asc" | "desc";
export type PageSort<K extends string> = { key: K; dir: SortDir };

/** The column a header click asked for: the one in TanStack's proposed state that is not the current key, else
 * the current key (a click on the sorted column, including TanStack's "remove the sort" step). */
export function clickedSortKey(next: readonly { id: string }[], current: string): string {
  return next.find((s) => s.id !== current)?.id ?? current;
}

/** The page's next sort after a header click: the same column flips, another column starts at `firstDir(key)`. */
export function nextPageSort<K extends string>(
  next: readonly { id: string }[],
  current: PageSort<K>,
  firstDir: (key: K) => SortDir,
): PageSort<K> {
  const key = clickedSortKey(next, current.key) as K;
  if (key === current.key) return { key, dir: current.dir === "asc" ? "desc" : "asc" };
  return { key, dir: firstDir(key) };
}

/** Kit DataTable props for a page-owned sort (`{...pageSortProps(sort, setSort, firstDir)}`). */
export function pageSortProps<K extends string>(
  sort: PageSort<K>,
  set: (s: PageSort<K>) => void,
  firstDir: (key: K) => SortDir,
) {
  return {
    sorting: [{ id: sort.key, desc: sort.dir === "desc" }],
    onSortingChange: (next: { id: string; desc: boolean }[]) => set(nextPageSort(next, sort, firstDir)),
    manualSorting: true as const,
  };
}

/** On a phone, `titleId` moves to the front so the kit card's title is that column; the desktop order is kept. */
export function phoneCardColumns<C extends { id?: string }>(columns: C[], titleId: string, isPhone: boolean): C[] {
  if (!isPhone) return columns;
  const i = columns.findIndex((c) => c.id === titleId);
  if (i <= 0) return columns;
  return [columns[i], ...columns.slice(0, i), ...columns.slice(i + 1)];
}
