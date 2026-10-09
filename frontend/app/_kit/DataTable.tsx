"use client";
// app/_kit/DataTable.tsx
// AA-662 — the shared table for every UI-v2 page, built on TanStack Table (@tanstack/react-table v8).
//
// Features (all optional, enabled by props):
//   - per-column filtering (global search box; column filters via column meta)
//   - multi-sort (shift-click headers)
//   - column show/hide menu
//   - client-side pagination with a sticky footer
//   - sticky header
//   - row selection + a bulk-action bar
//   - saved views (localStorage, per user)
//   - CSV export of the current (filtered) rows
//
// Client-side by default (filter/sort/paginate the rows it is given). AA-739: pass
// `serverPagination` when the page holds only one server page of a larger set — the pager and the
// page-size select then drive the server (pageIndex/pageSize/total come from the caller). Search
// and column sort still act on the rows of the current page only.

import {
  ColumnDef,
  ColumnFiltersState,
  SortingState,
  VisibilityState,
  flexRender,
  getCoreRowModel,
  getFilteredRowModel,
  getPaginationRowModel,
  getSortedRowModel,
  useReactTable,
  type RowSelectionState,
  type Table as RTable,
} from "@tanstack/react-table";
import {
  ArrowDown,
  ArrowUp,
  ChevronLeft,
  ChevronRight,
  Columns3,
  Download,
  Save,
  Search,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { downloadCsv, toCsv } from "./csv";
import { K, RADIUS, sans } from "./tokens";
import { EmptyState, TableSkeleton } from "./primitives";
import {
  deleteView,
  listViews,
  saveView,
  type SavedView,
} from "./savedViews";

type PersistedState = {
  sorting: SortingState;
  columnVisibility: VisibilityState;
  columnFilters: ColumnFiltersState;
  globalFilter: string;
  pageSize: number;
};

export type DataTableProps<T> = {
  data: T[];
  columns: ColumnDef<T, unknown>[];
  /** Stable id — namespaces saved views + column-visibility persistence. */
  tableId: string;
  getRowId: (row: T) => string;

  loading?: boolean;
  error?: string | null;
  onRetry?: () => void;

  // Search
  searchable?: boolean;
  searchPlaceholder?: string;

  // Selection + bulk
  enableSelection?: boolean;
  bulkActions?: (ctx: {
    selectedRows: T[];
    clearSelection: () => void;
  }) => React.ReactNode;

  // Pagination
  pageSize?: number;
  pageSizeOptions?: number[];
  /** AA-739: server-side pagination. `data` is the current server page; `total` is the row count
   * across all pages (after server filters). The pager calls `onChange(pageIndex, pageSize)`. */
  serverPagination?: {
    pageIndex: number;
    pageSize: number;
    total: number;
    onChange: (pageIndex: number, pageSize: number) => void;
  };

  // Saved views
  enableSavedViews?: boolean;
  userId?: string;

  // CSV
  enableCsv?: boolean;
  csvFilename?: string;

  // Empty state
  emptyTitle?: string;
  emptyDescription?: string;

  // Extra toolbar content (filters etc.), rendered left of the built-in toolbar buttons.
  toolbarExtra?: React.ReactNode;

  stickyHeader?: boolean;
  maxBodyHeight?: number | string;

  /** Optional: make rows clickable (e.g. open a detail drawer). Cells that stopPropagation (like
   * an actions cell) won't trigger it. */
  onRowClick?: (row: T) => void;
};

const toolbarBtn: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 6,
  padding: "7px 12px",
  borderRadius: RADIUS.pill,
  border: `1px solid ${K.line}`,
  background: K.card,
  color: K.ink3,
  fontSize: 12,
  fontWeight: 600,
  cursor: "pointer",
  fontFamily: sans,
};

export function DataTable<T>(props: DataTableProps<T>) {
  const {
    data,
    columns,
    tableId,
    getRowId,
    loading = false,
    error = null,
    onRetry,
    searchable = true,
    searchPlaceholder = "Search…",
    enableSelection = false,
    bulkActions,
    pageSize = 25,
    pageSizeOptions = [10, 25, 50, 100],
    serverPagination,
    enableSavedViews = false,
    userId = "anon",
    enableCsv = false,
    csvFilename = "export",
    emptyTitle = "Nothing here yet",
    emptyDescription,
    toolbarExtra,
    stickyHeader = true,
    maxBodyHeight,
    onRowClick,
  } = props;

  const [sorting, setSorting] = useState<SortingState>([]);
  const [columnFilters, setColumnFilters] = useState<ColumnFiltersState>([]);
  const [columnVisibility, setColumnVisibility] = useState<VisibilityState>({});
  const [globalFilter, setGlobalFilter] = useState("");
  const [rowSelection, setRowSelection] = useState<RowSelectionState>({});
  const [showCols, setShowCols] = useState(false);
  const [showViews, setShowViews] = useState(false);
  const [views, setViews] = useState<SavedView<PersistedState>[]>([]);
  const [pageSizeState, setPageSizeState] = useState(pageSize);

  // Load saved views once on mount (client only).
  useEffect(() => {
    if (enableSavedViews) {
      setViews(listViews<PersistedState>(tableId, userId));
    }
  }, [enableSavedViews, tableId, userId]);

  const selectCol: ColumnDef<T, unknown> | null = useMemo(() => {
    if (!enableSelection) return null;
    return {
      id: "__select",
      size: 36,
      enableSorting: false,
      enableHiding: false,
      header: ({ table }: { table: RTable<T> }) => (
        <input
          type="checkbox"
          aria-label="Select all"
          checked={table.getIsAllRowsSelected()}
          ref={(el) => {
            if (el) el.indeterminate = table.getIsSomeRowsSelected();
          }}
          onChange={table.getToggleAllRowsSelectedHandler()}
          style={{ cursor: "pointer" }}
        />
      ),
      cell: ({ row }) => (
        <input
          type="checkbox"
          aria-label="Select row"
          checked={row.getIsSelected()}
          onChange={row.getToggleSelectedHandler()}
          onClick={(e) => e.stopPropagation()}
          style={{ cursor: "pointer" }}
        />
      ),
    };
  }, [enableSelection]);

  const allColumns = useMemo(
    () => (selectCol ? [selectCol, ...columns] : columns),
    [selectCol, columns],
  );

  const sp = serverPagination;
  const table = useReactTable<T>({
    data,
    columns: allColumns,
    state: {
      sorting, columnFilters, columnVisibility, globalFilter, rowSelection,
      ...(sp ? { pagination: { pageIndex: sp.pageIndex, pageSize: sp.pageSize } } : {}),
    },
    ...(sp
      ? {
          manualPagination: true,
          pageCount: Math.max(1, Math.ceil(sp.total / Math.max(1, sp.pageSize))),
          onPaginationChange: (updater) => {
            const cur = { pageIndex: sp.pageIndex, pageSize: sp.pageSize };
            const next = typeof updater === "function" ? updater(cur) : updater;
            sp.onChange(next.pageIndex, next.pageSize);
          },
        }
      : {}),
    getRowId: (row) => getRowId(row),
    enableRowSelection: enableSelection,
    enableMultiSort: true,
    onSortingChange: setSorting,
    onColumnFiltersChange: setColumnFilters,
    onColumnVisibilityChange: setColumnVisibility,
    onGlobalFilterChange: setGlobalFilter,
    onRowSelectionChange: setRowSelection,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
  });

  // Keep the table's page size in sync with the local control (client mode only — in server mode
  // the caller owns pageSize and setPageSize would round-trip through onChange).
  useEffect(() => {
    if (!sp) table.setPageSize(pageSizeState);
  }, [pageSizeState, table, sp]);

  const selectedRows = table.getSelectedRowModel().rows.map((r) => r.original);
  const clearSelection = () => setRowSelection({});

  // ── Saved views ──
  const applyView = (v: SavedView<PersistedState>) => {
    setSorting(v.state.sorting ?? []);
    setColumnVisibility(v.state.columnVisibility ?? {});
    setColumnFilters(v.state.columnFilters ?? []);
    setGlobalFilter(v.state.globalFilter ?? "");
    setPageSizeState(v.state.pageSize ?? pageSize);
    setShowViews(false);
  };
  const handleSaveView = () => {
    const name = typeof window !== "undefined" ? window.prompt("Save view as:") : null;
    if (!name) return;
    const next = saveView<PersistedState>(tableId, userId, name, {
      sorting,
      columnVisibility,
      columnFilters,
      globalFilter,
      pageSize: pageSizeState,
    });
    setViews(next);
  };
  const handleDeleteView = (id: string) => {
    setViews(deleteView<PersistedState>(tableId, userId, id));
  };

  // ── CSV ──
  const exportCsv = () => {
    const rows = table.getFilteredRowModel().rows.map((r) => r.original);
    const cols = table
      .getVisibleLeafColumns()
      .filter((c) => c.id !== "__select")
      .map((c) => ({
        header:
          typeof c.columnDef.header === "string" ? c.columnDef.header : c.id,
        value: (row: T) => {
          const accessorFn = (c.columnDef as { accessorFn?: (r: T) => unknown }).accessorFn;
          const accessorKey = (c.columnDef as { accessorKey?: string }).accessorKey;
          if (accessorFn) return accessorFn(row);
          if (accessorKey) return (row as Record<string, unknown>)[accessorKey];
          return "";
        },
      }));
    downloadCsv(csvFilename, toCsv(rows, cols));
  };

  const hideMenuRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const onDoc = (e: MouseEvent) => {
      if (hideMenuRef.current && !hideMenuRef.current.contains(e.target as Node)) {
        setShowCols(false);
        setShowViews(false);
      }
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, []);

  const totalFiltered = sp ? sp.total : table.getFilteredRowModel().rows.length;
  const pageRows = table.getRowModel().rows;
  const pageIndex = table.getState().pagination.pageIndex;
  const pageCount = table.getPageCount();
  const effPageSize = sp ? sp.pageSize : pageSizeState;

  return (
    <div style={{ fontFamily: sans, position: "relative" }}>
      {/* Toolbar */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 10,
          marginBottom: 12,
          flexWrap: "wrap",
        }}
      >
        {searchable && (
          <div style={{ position: "relative" }}>
            <Search
              size={14}
              style={{
                position: "absolute",
                left: 11,
                top: "50%",
                transform: "translateY(-50%)",
                color: K.muted2,
              }}
            />
            <input
              value={globalFilter}
              onChange={(e) => setGlobalFilter(e.target.value)}
              placeholder={searchPlaceholder}
              style={{
                padding: "7px 12px 7px 32px",
                borderRadius: RADIUS.pill,
                border: `1px solid ${K.line}`,
                fontSize: 13,
                fontFamily: sans,
                color: K.ink,
                minWidth: 220,
                outline: "none",
              }}
            />
          </div>
        )}

        {toolbarExtra}

        <div style={{ flex: 1 }} />

        {enableSavedViews && (
          <div ref={hideMenuRef} style={{ position: "relative" }}>
            <button style={toolbarBtn} onClick={() => { setShowViews((v) => !v); setShowCols(false); }}>
              <Save size={13} /> Views
            </button>
            {showViews && (
              <div style={menuBox}>
                <button style={menuItem} onClick={handleSaveView}>
                  + Save current view…
                </button>
                {views.length === 0 && (
                  <div style={{ ...menuItem, color: K.muted2, cursor: "default" }}>
                    No saved views
                  </div>
                )}
                {views.map((v) => (
                  <div
                    key={v.id}
                    style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}
                  >
                    <button style={{ ...menuItem, flex: 1 }} onClick={() => applyView(v)}>
                      {v.name}
                    </button>
                    <button
                      style={{ ...menuItem, color: K.danger, width: "auto" }}
                      onClick={() => handleDeleteView(v.id)}
                      aria-label={`Delete ${v.name}`}
                    >
                      ×
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        <div style={{ position: "relative" }}>
          <button style={toolbarBtn} onClick={() => { setShowCols((v) => !v); setShowViews(false); }}>
            <Columns3 size={13} /> Columns
          </button>
          {showCols && (
            <div style={menuBox}>
              {table
                .getAllLeafColumns()
                .filter((c) => c.id !== "__select" && c.getCanHide())
                .map((c) => (
                  <label key={c.id} style={{ ...menuItem, display: "flex", gap: 8, alignItems: "center" }}>
                    <input
                      type="checkbox"
                      checked={c.getIsVisible()}
                      onChange={c.getToggleVisibilityHandler()}
                    />
                    {typeof c.columnDef.header === "string" ? c.columnDef.header : c.id}
                  </label>
                ))}
            </div>
          )}
        </div>

        {enableCsv && (
          <button style={toolbarBtn} onClick={exportCsv}>
            <Download size={13} /> CSV
          </button>
        )}
      </div>

      {/* Bulk action bar */}
      {enableSelection && selectedRows.length > 0 && (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 12,
            padding: "10px 14px",
            marginBottom: 10,
            borderRadius: RADIUS.md,
            background: K.accentTint,
            border: `1px solid ${K.accentBorder}`,
          }}
        >
          <span style={{ fontSize: 13, fontWeight: 600, color: K.accentDeep }}>
            {selectedRows.length} selected
          </span>
          <div style={{ display: "flex", gap: 8, flex: 1 }}>
            {bulkActions?.({ selectedRows, clearSelection })}
          </div>
          <button
            style={{ ...toolbarBtn, border: "none", background: "transparent", color: K.accentDeep }}
            onClick={clearSelection}
          >
            Clear
          </button>
        </div>
      )}

      {/* Body */}
      {error ? (
        <div style={{ padding: 24 }}>
          <EmptyState
            title="Could not load"
            description={error}
            action={
              onRetry && (
                <button style={toolbarBtn} onClick={onRetry}>
                  Retry
                </button>
              )
            }
          />
        </div>
      ) : loading ? (
        <div style={{ padding: 16 }}>
          <TableSkeleton rows={6} cols={Math.min(allColumns.length, 5)} />
        </div>
      ) : totalFiltered === 0 ? (
        <EmptyState title={emptyTitle} description={emptyDescription} />
      ) : (
        <div
          style={{
            border: `1px solid ${K.line}`,
            borderRadius: RADIUS.lg,
            overflow: "auto",
            maxHeight: maxBodyHeight,
            background: K.card,
          }}
        >
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              {table.getHeaderGroups().map((hg) => (
                <tr key={hg.id}>
                  {hg.headers.map((header) => {
                    const canSort = header.column.getCanSort();
                    const sorted = header.column.getIsSorted();
                    return (
                      <th
                        key={header.id}
                        onClick={canSort ? header.column.getToggleSortingHandler() : undefined}
                        style={{
                          padding: "10px 16px",
                          fontSize: 11,
                          fontWeight: 600,
                          textTransform: "uppercase",
                          letterSpacing: "0.1em",
                          color: K.muted,
                          textAlign: "left",
                          background: K.bg,
                          borderBottom: `1px solid ${K.line}`,
                          cursor: canSort ? "pointer" : "default",
                          userSelect: "none",
                          position: stickyHeader ? "sticky" : undefined,
                          top: stickyHeader ? 0 : undefined,
                          zIndex: stickyHeader ? 1 : undefined,
                          whiteSpace: "nowrap",
                        }}
                      >
                        <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                          {flexRender(header.column.columnDef.header, header.getContext())}
                          {sorted === "asc" && <ArrowUp size={12} />}
                          {sorted === "desc" && <ArrowDown size={12} />}
                        </span>
                      </th>
                    );
                  })}
                </tr>
              ))}
            </thead>
            <tbody data-testid="kit-datatable-body">
              {pageRows.map((row) => (
                <tr
                  key={row.id}
                  data-testid="kit-datatable-row"
                  onClick={onRowClick ? () => onRowClick(row.original) : undefined}
                  style={{
                    background: row.getIsSelected() ? K.accentTint : "transparent",
                    cursor: onRowClick ? "pointer" : undefined,
                  }}
                >
                  {row.getVisibleCells().map((cell) => (
                    <td
                      key={cell.id}
                      style={{
                        padding: "13px 16px",
                        fontSize: 13,
                        color: K.body,
                        borderBottom: `1px solid ${K.line2}`,
                      }}
                    >
                      {flexRender(cell.column.columnDef.cell, cell.getContext())}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Footer / pagination */}
      {!loading && !error && totalFiltered > 0 && (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 12,
            marginTop: 12,
            flexWrap: "wrap",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 10, fontSize: 12, color: K.muted }}>
            <span>
              {pageIndex * effPageSize + 1}–{Math.min((pageIndex + 1) * effPageSize, totalFiltered)} of{" "}
              {totalFiltered}
            </span>
            <select
              value={effPageSize}
              onChange={(e) =>
                sp ? sp.onChange(0, Number(e.target.value)) : setPageSizeState(Number(e.target.value))
              }
              style={{
                padding: "4px 8px",
                borderRadius: RADIUS.sm,
                border: `1px solid ${K.line}`,
                fontSize: 12,
                fontFamily: sans,
                color: K.ink3,
                cursor: "pointer",
              }}
            >
              {pageSizeOptions.map((n) => (
                <option key={n} value={n}>
                  {n} / page
                </option>
              ))}
            </select>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <button
              style={{ ...toolbarBtn, padding: "6px 10px", opacity: table.getCanPreviousPage() ? 1 : 0.4 }}
              onClick={() => table.previousPage()}
              disabled={!table.getCanPreviousPage()}
            >
              <ChevronLeft size={14} />
            </button>
            <span style={{ fontSize: 12, color: K.muted, minWidth: 70, textAlign: "center" }}>
              Page {pageIndex + 1} / {Math.max(pageCount, 1)}
            </span>
            <button
              style={{ ...toolbarBtn, padding: "6px 10px", opacity: table.getCanNextPage() ? 1 : 0.4 }}
              onClick={() => table.nextPage()}
              disabled={!table.getCanNextPage()}
            >
              <ChevronRight size={14} />
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

const menuBox: React.CSSProperties = {
  position: "absolute",
  top: "calc(100% + 6px)",
  right: 0,
  minWidth: 200,
  background: K.card,
  border: `1px solid ${K.line}`,
  borderRadius: RADIUS.md,
  boxShadow: "0 10px 30px rgba(0,0,0,0.12)",
  padding: 6,
  zIndex: 50,
  maxHeight: 300,
  overflow: "auto",
};

const menuItem: React.CSSProperties = {
  display: "block",
  width: "100%",
  textAlign: "left",
  padding: "7px 10px",
  borderRadius: RADIUS.sm,
  border: "none",
  background: "transparent",
  fontSize: 13,
  color: K.ink3,
  cursor: "pointer",
  fontFamily: sans,
};

// Re-export so callers can type their column definitions without importing react-table directly.
export type { ColumnDef } from "@tanstack/react-table";
export { createColumnHelper } from "@tanstack/react-table";
