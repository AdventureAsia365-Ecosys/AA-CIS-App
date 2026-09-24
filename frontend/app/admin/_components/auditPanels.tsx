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
import { useState } from "react";
import { AlertTriangle, ClipboardList, RotateCw } from "lucide-react";
import { A, sans, Card, Btn, TH, TD } from "./adminUi";

export async function fetchJson<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url} → HTTP ${res.status}`);
  return res.json();
}

export function EmptyState({ title, body }: { title: string; body: string }) {
  return (
    <Card>
      <div style={{ textAlign: "center", padding: "40px 20px" }}>
        <ClipboardList size={30} color={A.accent} style={{ marginBottom: 10 }} />
        <div style={{ fontSize: 15, fontWeight: 600, color: A.ink, marginBottom: 6 }}>{title}</div>
        <div style={{ fontSize: 13, color: A.muted }}>{body}</div>
      </div>
    </Card>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <Card style={{ borderColor: A.redBorder }}>
      <div style={{ textAlign: "center", padding: "32px 20px" }}>
        <AlertTriangle size={26} color={A.red} style={{ marginBottom: 10 }} />
        <div style={{ fontSize: 14, fontWeight: 600, color: A.ink, marginBottom: 6 }}>Could not load this panel</div>
        <div style={{ fontSize: 12.5, color: A.muted, marginBottom: 14 }}>{message}</div>
        <Btn variant="secondary" size="sm" onClick={onRetry}><RotateCw size={13} /> Retry</Btn>
      </div>
    </Card>
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
export function AuditTable<T>({ rows, columns, rowKey, sortable = false }: {
  rows: T[]; columns: Col<T>[]; rowKey: (row: T) => string; sortable?: boolean;
}) {
  const [sortKey, setSortKey] = useState<string | null>(null);
  const [sortDir, setSortDir] = useState<"asc" | "desc">("asc");
  const [colFilters, setColFilters] = useState<Record<string, string>>({});

  let displayRows = rows;
  if (sortable) {
    displayRows = rows.filter(r => columns.every(c => {
      const f = colFilters[c.key];
      if (!f || !c.filterValue) return true;
      return c.filterValue(r).toLowerCase().includes(f.toLowerCase());
    }));
    const sortCol = sortKey ? columns.find(c => c.key === sortKey) : undefined;
    if (sortCol?.sortValue) {
      const sv = sortCol.sortValue;
      displayRows = [...displayRows].sort((a, b) => {
        const av = sv(a), bv = sv(b);
        if (av == null && bv == null) return 0;
        if (av == null) return 1;
        if (bv == null) return -1;
        const cmp = typeof av === "number" && typeof bv === "number" ? av - bv : String(av).localeCompare(String(bv));
        return sortDir === "asc" ? cmp : -cmp;
      });
    }
  }

  function toggleSort(key: string) {
    if (sortKey !== key) { setSortKey(key); setSortDir("asc"); }
    else if (sortDir === "asc") setSortDir("desc");
    else { setSortKey(null); setSortDir("asc"); }
  }

  const hasFilterRow = sortable && columns.some(c => c.filterValue);

  return (
    <div style={{ overflowX: "auto", border: `1px solid ${A.line}`, borderRadius: 10 }}>
      <table style={{ width: "100%", borderCollapse: "collapse", fontFamily: sans }}>
        <thead>
          <tr>
            {columns.map(c => (
              <th key={c.key} style={TH}>
                {sortable && c.sortValue ? (
                  <button onClick={() => toggleSort(c.key)} style={{
                    background: "none", border: "none", padding: 0, cursor: "pointer",
                    font: "inherit", color: sortKey === c.key ? A.gold : "inherit",
                    display: "flex", alignItems: "center", gap: 4,
                  }}>
                    {c.label}
                    <span style={{ fontSize: 9, opacity: sortKey === c.key ? 1 : 0.35 }}>
                      {sortKey === c.key ? (sortDir === "asc" ? "▲" : "▼") : "▲▼"}
                    </span>
                  </button>
                ) : c.label}
              </th>
            ))}
          </tr>
          {hasFilterRow && (
            <tr>
              {columns.map(c => (
                <th key={`${c.key}-filter`} style={{ ...TH, padding: "4px 16px 8px", textTransform: "none", fontWeight: 400 }}>
                  {c.filterValue && (
                    <input
                      value={colFilters[c.key] ?? ""}
                      onChange={e => setColFilters(prev => ({ ...prev, [c.key]: e.target.value }))}
                      placeholder="Filter…"
                      style={{
                        width: "100%", fontSize: 11.5, padding: "4px 7px", boxSizing: "border-box",
                        border: `1px solid ${A.line}`, borderRadius: 5, background: A.card, color: A.body,
                      }}
                    />
                  )}
                </th>
              ))}
            </tr>
          )}
        </thead>
        <tbody>
          {displayRows.map(row => (
            <tr key={rowKey(row)}>
              {columns.map(c => <td key={c.key} style={TD}>{c.render(row)}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
      {sortable && displayRows.length === 0 && rows.length > 0 && (
        <div style={{ padding: "16px", textAlign: "center", fontSize: 12.5, color: A.muted2 }}>
          No rows match the current column filter(s).
        </div>
      )}
    </div>
  );
}
