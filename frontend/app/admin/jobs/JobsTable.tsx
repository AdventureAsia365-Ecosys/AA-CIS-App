"use client";
// app/admin/jobs/JobsTable.tsx — AA-755. The jobs DataTable, split out of page.tsx so the page can
// put it in a tab next to the worker/queue panel. Pure view: it is handed the current server page
// of jobs plus the server pagination/sort state and the cancel/retry handlers; the page owns the
// react-query fetch and the 10 s poll. Server sort maps column clicks to the API's sort whitelist
// (AA-755); when the API is older and returns no `total`, the page passes a client fallback and the
// sort silently stays on the server default (created_at desc).

import { useMemo } from "react";
import type { SortingState } from "@tanstack/react-table";
import { A, mono, Btn } from "../_components/adminUi";
import { Badge, type ColumnDef, DataTable, StatusBadge } from "../../_kit";
import {
  STATUSES, fmtSeconds, fmtTime, releases, runSeconds, stepProgress, usd,
  type Job, type Kind,
} from "./jobsShared";

// The columns that map to a server sort key (AA-755: queue.JOB_SORTS). Clicking any other header is
// a no-op (enableSorting:false). TanStack sends the column id; we translate it to sort/sort_dir.
const SERVER_SORT_IDS = new Set(["kind", "status", "cost_usd", "duration", "created_at"]);

export interface JobsTableProps {
  jobs: Job[];
  kinds: Kind[];
  etas: Record<string, string | null>;
  now: number;
  maxReleases: number;
  busyId: string | null;
  // Server-driven pagination (DataTable serverPagination). `total` is the row count across all
  // pages after the server filters; `supportsServer` is false when the API returned no `total`.
  page: number;        // 0-based
  pageSize: number;
  total: number;
  supportsServer: boolean;
  onPageChange: (pageIndex: number, pageSize: number) => void;
  // Server sort (AA-755). `sort`/`sortDir` reflect the current server order.
  sort: string;
  sortDir: "asc" | "desc";
  onSortChange: (sort: string, dir: "asc" | "desc") => void;
  // Filters rendered in the toolbar.
  kind: string;
  status: string;
  onKindChange: (k: string) => void;
  onStatusChange: (s: string) => void;
  onRowClick: (id: string) => void;
  onAct: (job: Job, action: "cancel" | "retry") => void;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
}

export default function JobsTable(props: JobsTableProps) {
  const {
    jobs, kinds, etas, now, maxReleases, busyId,
    page, pageSize, total, supportsServer, onPageChange,
    sort, sortDir, onSortChange,
    kind, status, onKindChange, onStatusChange,
    onRowClick, onAct, loading, error, onRetry,
  } = props;

  const columns = useMemo<ColumnDef<Job, unknown>[]>(() => {
    const expected = (k: string) => kinds.find(x => x.kind === k)?.expected_seconds;
    return [
    {
      accessorKey: "kind",
      header: "Kind",
      cell: (c) => {
        const j = c.row.original;
        const secs = runSeconds(j, now);
        const exp = expected(j.kind);
        const slow = j.status === "running" && exp != null && secs != null && secs > exp;
        const rel = releases(j);
        return (
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            <span style={{ fontFamily: mono, fontWeight: 600, color: A.ink }}>{j.kind}</span>
            {slow && <span title={`Expected under ${fmtSeconds(exp!)}`}><Badge tone="warning" dot={false}>slow</Badge></span>}
            {rel > 0 && (j.status === "running" || j.status === "queued") && (
              <span title="Handed back to the queue by a deploy/restart">
                <Badge tone={rel >= maxReleases - 1 ? "danger" : "neutral"} dot={false}>released {rel}/{maxReleases}</Badge>
              </span>
            )}
          </div>
        );
      },
    },
    {
      accessorKey: "status",
      header: "Status",
      cell: (c) => {
        const j = c.row.original;
        return (
          <span style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
            <StatusBadge status={j.status} />
            {j.cancel_requested && j.status === "running" && <span style={{ fontSize: 11, color: A.muted }}>cancelling…</span>}
          </span>
        );
      },
    },
    {
      id: "attempt",
      header: "Attempt",
      enableSorting: false,
      cell: (c) => <span style={{ fontFamily: mono }}>{c.row.original.attempt}/{c.row.original.max_attempts}</span>,
    },
    {
      id: "progress",
      header: "Progress",
      enableSorting: false,
      cell: (c) => {
        const j = c.row.original;
        if (j.error && j.status !== "succeeded") return <span style={{ color: A.red }}>{j.error}</span>;
        const p = stepProgress(j);
        const phase = typeof j.progress?.phase === "string" ? j.progress.phase : "";
        if (!p) return <span style={{ color: A.muted }}>{phase || "—"}</span>;
        const pct = Math.min(100, (p.done / p.total) * 100);
        const eta = etas[j.id] ?? null;
        return (
          <div style={{ minWidth: 150 }}>
            <div style={{ fontSize: 11.5, color: A.body }}>
              {p.step || phase} · <span style={{ fontFamily: mono }}>{p.done}/{p.total}</span>
              {j.status === "running" && eta && <span style={{ color: A.muted }}> · ETA {eta}</span>}
            </div>
            <div style={{ height: 4, background: A.line2, borderRadius: 2, overflow: "hidden", marginTop: 3 }}>
              <div style={{ width: `${pct}%`, height: "100%", background: A.accent }} />
            </div>
          </div>
        );
      },
    },
    {
      id: "duration",
      header: "Duration",
      cell: (c) => {
        const j = c.row.original;
        const secs = runSeconds(j, now);
        const exp = expected(j.kind);
        const slow = j.status === "running" && exp != null && secs != null && secs > exp;
        return <span style={{ fontFamily: mono, color: slow ? A.amber : A.body }}>{secs == null ? "—" : fmtSeconds(secs)}</span>;
      },
    },
    {
      accessorKey: "cost_usd",
      header: "Cost",
      cell: (c) => <span style={{ fontFamily: mono }}>{usd(c.row.original.cost_usd)}</span>,
    },
    {
      id: "created_at",
      accessorKey: "created_at",
      header: "Created",
      cell: (c) => <span style={{ color: A.muted, whiteSpace: "nowrap" }}>{fmtTime(c.row.original.created_at)}</span>,
    },
    {
      accessorKey: "created_by",
      header: "By",
      enableSorting: false,
      cell: (c) => <span style={{ color: A.muted }}>{c.row.original.created_by ?? "—"}</span>,
    },
    {
      id: "actions",
      header: "",
      enableSorting: false,
      enableHiding: false,
      cell: (c) => {
        const j = c.row.original;
        const canCancel = j.status === "queued" || (j.status === "running" && !j.cancel_requested);
        const canRetry = j.status === "failed" || j.status === "stopped_budget" || j.status === "cancelled";
        if (!canCancel && !canRetry) return null;
        return (
          <div style={{ display: "flex", gap: 8, whiteSpace: "nowrap" }} onClick={(e) => e.stopPropagation()}>
            {canCancel && <Btn size="sm" variant="danger" disabled={busyId === j.id} onClick={() => onAct(j, "cancel")}>Cancel</Btn>}
            {canRetry && <Btn size="sm" disabled={busyId === j.id} onClick={() => onAct(j, "retry")}>Retry</Btn>}
          </div>
        );
      },
    },
  ];
  }, [now, etas, maxReleases, busyId, kinds, onAct]);

  // Reflect the server sort to the DataTable so the sorted-arrow shows on the active column, and
  // translate a header click back into a (sort, dir) the page sends to the API. Column ids outside
  // the whitelist stay unsorted (enableSorting:false), so this only ever yields a server key.
  const sorting: SortingState = supportsServer && SERVER_SORT_IDS.has(sort)
    ? [{ id: sort, desc: sortDir === "desc" }]
    : [];
  const handleSortingChange = (next: SortingState) => {
    if (!supportsServer) return;
    const s = next[0];
    if (!s) { onSortChange("created_at", "desc"); return; }
    onSortChange(s.id, s.desc ? "desc" : "asc");
  };

  const serverPagination = supportsServer
    ? { pageIndex: page, pageSize, total, onChange: onPageChange }
    : undefined;

  return (
    <DataTable<Job>
      data={jobs}
      columns={columns}
      tableId="admin-jobs"
      getRowId={(j) => j.id}
      loading={loading}
      error={error}
      onRetry={onRetry}
      searchable
      searchPlaceholder="Search this page…"
      enableSavedViews
      enableCsv
      csvFilename="jobs"
      pageSize={pageSize}
      pageSizeOptions={[25, 50, 100]}
      serverPagination={serverPagination}
      manualSorting={supportsServer}
      sorting={sorting}
      onSortingChange={handleSortingChange}
      stickyHeader
      maxBodyHeight="calc(100vh - 360px)"
      tableMinWidth={980}
      emptyTitle="No jobs match these filters yet"
      emptyDescription="Background work shows here as it is enqueued."
      onRowClick={(j) => onRowClick(j.id)}
      toolbarExtra={
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <select value={kind} onChange={e => onKindChange(e.target.value)} aria-label="Kind"
                  style={{ fontSize: 12, padding: "6px 8px", border: `1px solid ${A.line}`, borderRadius: 6, background: A.card, color: A.ink3, maxWidth: 220, minWidth: 0 }}>
            <option value="">All kinds</option>
            {kinds.map(k => <option key={k.kind} value={k.kind}>{k.kind}</option>)}
          </select>
          <select value={status} onChange={e => onStatusChange(e.target.value)} aria-label="Status"
                  style={{ fontSize: 12, padding: "6px 8px", border: `1px solid ${A.line}`, borderRadius: 6, background: A.card, color: A.ink3 }}>
            <option value="">All statuses</option>
            {STATUSES.map(s => <option key={s} value={s}>{s}</option>)}
          </select>
        </div>
      }
    />
  );
}
