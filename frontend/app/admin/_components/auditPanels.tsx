// app/admin/_components/auditPanels.tsx — AA-551, trimmed AA-568.
//
// Generic, reusable admin-table helpers shared by `/admin/atom-curation` (01-05) and
// `/admin/tenant-activity` (06 · Content Trace). AA-568 removed this file's other half — the
// Write/Gate + Review + Publish section components (`WriteGateSection`/`ReviewSection`/
// `PublishSection`/`PieceLineageCard`/`useContentLog`/`useTourScopedFetch`/`CrossLinkNote`/
// `PickTourPrompt`/`ContentLogRow`/`PublishRow`/`sourceLabel`) that used to live here — those 3
// old tabs read the exact same `/admin/a4/content-log` dataset twice (AA-558's confirmed
// finding) and are now ONE merged table + row-accordion, built directly in
// `tenant-activity/page.tsx` (its own types/component, no longer generic enough to share).
import { useMemo, useState } from "react";
import { AlertTriangle, ClipboardList, RotateCw } from "lucide-react";
import type { ColumnDef } from "@tanstack/react-table";
import { useT } from "../../_kit/core/useI18n";
import { M } from "./components.messages";
import { Button, DataTable, Field, Input, K, Section } from "../../_kit";

export async function fetchJson<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url} → HTTP ${res.status}`);
  return res.json();
}

export function EmptyState({ title, body }: { title: string; body: string }) {
  return (
    <Section>
      <div style={{ textAlign: "center", padding: "40px 20px" }}>
        <ClipboardList size={30} color={K.accent} style={{ marginBottom: 10 }} />
        <div style={{ fontSize: 15, fontWeight: 600, color: K.ink, marginBottom: 6 }}>{title}</div>
        <div style={{ fontSize: 13, color: K.muted }}>{body}</div>
      </div>
    </Section>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry: () => void }) {
  const t = useT(M);
  return (
    <Section style={{ borderColor: K.dangerBorder }}>
      <div style={{ textAlign: "center", padding: "32px 20px" }}>
        <AlertTriangle size={26} color={K.danger} style={{ marginBottom: 10 }} />
        <div style={{ fontSize: 14, fontWeight: 600, color: K.ink, marginBottom: 6 }}>{t("panelFailed")}</div>
        <div style={{ fontSize: 12.5, color: K.muted, marginBottom: 14 }}>{message}</div>
        <Button variant="secondary" size="sm" onClick={onRetry}><RotateCw size={13} /> {t("retry")}</Button>
      </div>
    </Section>
  );
}

// AA-557 D.6/E.8/F.10/G.13 — `sortValue`/`filterValue` are optional per-column accessors that
// opt a column into AuditTable's own sort/filter UI (see `sortable` prop below). Omitting both on
// every column (every EXISTING caller — Review/Publish sections above, tenant-activity's own
// tables) keeps AuditTable rendering byte-identical to before; no caller needed to change.
export interface Col<T> {
  key: string; label: string; render: (row: T) => React.ReactNode;
  sortValue?: (row: T) => string | number | null;
  filterValue?: (row: T) => string;
}

// AA-557 — client-side sort/filter over whatever page of rows is already loaded (same scope as
// the existing focusRouteId/focusSegmentId cross-filters in atom-curation/page.tsx, which also
// narrow the fetched page rather than adding a new backend query param per column). Real row
// counts here (Segment 138, Score ~138, Route handful) fit in 1-3 pages at PAGE_SIZE=50, so a
// per-page sort/filter is legible; it does NOT re-sort/filter across page boundaries — the
// existing pagination footer is unaffected.
//
// AA-1006: drawn by the kit DataTable (header sort with aria-sort, phone card mode). The per-column filters are kit
// Inputs in the table's toolbar (they were a second header row); a sortable column sorts by its `sortValue`, empty
// values last in both directions as before. No global search and no pager: the rows are the page already loaded.
export function AuditTable<T>({ rows, columns, rowKey, sortable = false, tableId }: {
  rows: T[]; columns: Col<T>[]; rowKey: (row: T) => string; sortable?: boolean;
  /** Namespaces the DataTable's column-visibility memory; defaults to the column keys. */
  tableId?: string;
}) {
  const t = useT(M);
  const [colFilters, setColFilters] = useState<Record<string, string>>({});

  const displayRows = useMemo(() => !sortable ? rows : rows.filter(r => columns.every(c => {
    const f = colFilters[c.key];
    if (!f || !c.filterValue) return true;
    return c.filterValue(r).toLowerCase().includes(f.toLowerCase());
  })), [rows, columns, colFilters, sortable]);

  const defs = useMemo<ColumnDef<T, unknown>[]>(() => columns.map((c) => {
    const sv = sortable ? c.sortValue : undefined;
    return {
      id: c.key,
      header: c.label,
      // null → undefined, so `sortUndefined: "last"` keeps empty values last in both directions.
      accessorFn: sv ? (row: T) => sv(row) ?? undefined : () => undefined,
      cell: (info) => c.render(info.row.original),
      enableSorting: !!sv,
      sortUndefined: "last",
      sortingFn: (a, b, id) => {
        const av = a.getValue(id) as string | number, bv = b.getValue(id) as string | number;
        return typeof av === "number" && typeof bv === "number" ? av - bv : String(av).localeCompare(String(bv));
      },
    } satisfies ColumnDef<T, unknown>;
  }), [columns, sortable]);

  const filterCols = sortable ? columns.filter(c => c.filterValue) : [];
  const filters = filterCols.length > 0 ? (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "flex-end" }}>
      {filterCols.map(c => (
        <Field key={c.key} label={c.label} style={{ width: 160 }}>
          <Input
            size="sm"
            value={colFilters[c.key] ?? ""}
            onChange={e => setColFilters(prev => ({ ...prev, [c.key]: e.target.value }))}
            placeholder={t("filter")}
            style={{ width: "100%" }}
          />
        </Field>
      ))}
    </div>
  ) : undefined;

  return (
    <DataTable<T>
      tableId={tableId ?? `audit:${columns.map(c => c.key).join(",")}`}
      data={displayRows}
      columns={defs}
      getRowId={rowKey}
      searchable={false}
      paginate={false}
      toolbarExtra={filters}
      emptyTitle={rows.length > 0 ? t("noRowsMatch") : undefined}
    />
  );
}
