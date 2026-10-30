"use client";
// app/admin/jobs/JobsTable.tsx — AA-755. The jobs DataTable, split out of page.tsx so the page can
// put it in a tab next to the worker/queue panel. Pure view: it is handed the current server page
// of jobs plus the server pagination/sort state and the cancel/retry handlers; the page owns the
// react-query fetch and the 10 s poll. Server sort maps column clicks to the API's sort whitelist
// (AA-755); when the API is older and returns no `total`, the page passes a client fallback and the
// sort silently stays on the server default (created_at desc).

import { useMemo } from "react";
import type { SortingState } from "@tanstack/react-table";
import { K, mono, Button, Badge, type ColumnDef, DataTable, StatusBadge, useFormat, useT } from "../../_kit";
import { JOBS } from "./messages";
import {
  STATUSES, fmtSeconds, releases, runSeconds, stepProgress, usd,
  type Job, type Kind,
} from "./jobsShared";

// The columns that map to a server sort key (AA-755: queue.JOB_SORTS). Clicking any other header is
// a no-op (enableSorting:false). TanStack sends the column id; we translate it to sort/sort_dir.
const SERVER_SORT_IDS = new Set(["kind", "status", "attempt", "cost_usd", "duration", "created_at", "created_by"]);

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
  pageSizeOptions: number[];
  // Server sort (AA-755). `sort`/`sortDir` reflect the current server order.
  sort: string;
  sortDir: "asc" | "desc";
  onSortChange: (sort: string, dir: "asc" | "desc") => void;
  // Filters rendered in the toolbar.
  kind: string;
  status: string;
  createdBy: string;
  createdByOptions: string[];
  since: string;
  until: string;
  q: string;
  onKindChange: (k: string) => void;
  onStatusChange: (s: string) => void;
  onCreatedByChange: (v: string) => void;
  onSinceChange: (v: string) => void;
  onUntilChange: (v: string) => void;
  onQChange: (v: string) => void;
  onRowClick: (id: string) => void;
  onAct: (job: Job, action: "cancel" | "retry") => void;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
}

export default function JobsTable(props: JobsTableProps) {
  const {
    jobs, kinds, etas, now, maxReleases, busyId,
    page, pageSize, pageSizeOptions, total, supportsServer, onPageChange,
    sort, sortDir, onSortChange,
    kind, status, createdBy, createdByOptions, since, until, q,
    onKindChange, onStatusChange, onCreatedByChange, onSinceChange, onUntilChange, onQChange,
    onRowClick, onAct, loading, error, onRetry,
  } = props;
  const t = useT(JOBS);
  const { formatDateTime: fmtTime } = useFormat();

  const inputStyle: React.CSSProperties = {
    fontSize: 12, padding: "6px 8px", border: `1px solid ${K.line}`, borderRadius: 6,
    background: K.card, color: K.ink3, minWidth: 0,
  };

  const columns = useMemo<ColumnDef<Job, unknown>[]>(() => {
    const expected = (k: string) => kinds.find(x => x.kind === k)?.expected_seconds;
    return [
    {
      accessorKey: "kind",
      header: t("colKind"),
      cell: (c) => {
        const j = c.row.original;
        const secs = runSeconds(j, now);
        const exp = expected(j.kind);
        const slow = j.status === "running" && exp != null && secs != null && secs > exp;
        const rel = releases(j);
        return (
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            <span style={{ fontFamily: mono, fontWeight: 600, color: K.ink }}>{j.kind}</span>
            {slow && <span title={t("expectedUnder", fmtSeconds(exp!))}><Badge tone="warning" dot={false}>{t("slow")}</Badge></span>}
            {rel > 0 && (j.status === "running" || j.status === "queued") && (
              <span title={t("releasedTitle")}>
                <Badge tone={rel >= maxReleases - 1 ? "danger" : "neutral"} dot={false}>{t("released", rel, maxReleases)}</Badge>
              </span>
            )}
          </div>
        );
      },
    },
    {
      accessorKey: "status",
      header: t("colStatus"),
      cell: (c) => {
        const j = c.row.original;
        return (
          <span style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
            <StatusBadge status={j.status} />
            {j.cancel_requested && j.status === "running" && <span style={{ fontSize: 11, color: K.muted }}>{t("cancelling")}</span>}
          </span>
        );
      },
    },
    {
      accessorKey: "attempt",
      header: t("colAttempt"),
      cell: (c) => <span style={{ fontFamily: mono }}>{c.row.original.attempt}/{c.row.original.max_attempts}</span>,
    },
    {
      id: "progress",
      header: t("colProgress"),
      enableSorting: false,
      cell: (c) => {
        const j = c.row.original;
        if (j.error && j.status !== "succeeded") return <span style={{ color: K.danger }}>{j.error}</span>;
        const p = stepProgress(j);
        const phase = typeof j.progress?.phase === "string" ? j.progress.phase : "";
        if (!p) return <span style={{ color: K.muted }}>{phase || "—"}</span>;
        const pct = Math.min(100, (p.done / p.total) * 100);
        const eta = etas[j.id] ?? null;
        return (
          <div style={{ minWidth: 150 }}>
            <div style={{ fontSize: 11.5, color: K.body }}>
              {p.step || phase} · <span style={{ fontFamily: mono }}>{p.done}/{p.total}</span>
              {j.status === "running" && eta && <span style={{ color: K.muted }}>{t("eta", eta)}</span>}
            </div>
            <div style={{ height: 4, background: K.line2, borderRadius: 2, overflow: "hidden", marginTop: 3 }}>
              <div style={{ width: `${pct}%`, height: "100%", background: K.accent }} />
            </div>
          </div>
        );
      },
    },
    {
      id: "duration",
      header: t("colDuration"),
      cell: (c) => {
        const j = c.row.original;
        const secs = runSeconds(j, now);
        const exp = expected(j.kind);
        const slow = j.status === "running" && exp != null && secs != null && secs > exp;
        return <span style={{ fontFamily: mono, color: slow ? K.amber : K.body }}>{secs == null ? "—" : fmtSeconds(secs)}</span>;
      },
    },
    {
      accessorKey: "cost_usd",
      header: t("colCost"),
      cell: (c) => <span style={{ fontFamily: mono }}>{usd(c.row.original.cost_usd)}</span>,
    },
    {
      id: "created_at",
      accessorKey: "created_at",
      header: t("colCreated"),
      cell: (c) => <span style={{ color: K.muted, whiteSpace: "nowrap" }}>{fmtTime(c.row.original.created_at)}</span>,
    },
    {
      accessorKey: "created_by",
      header: t("colBy"),
      cell: (c) => <span style={{ color: K.muted }}>{c.row.original.created_by ?? "—"}</span>,
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
            {canCancel && <Button size="sm" variant="danger" disabled={busyId === j.id} onClick={() => onAct(j, "cancel")}>{t("cancel")}</Button>}
            {canRetry && <Button size="sm" disabled={busyId === j.id} onClick={() => onAct(j, "retry")}>{t("retry")}</Button>}
          </div>
        );
      },
    },
  ];
  }, [now, etas, maxReleases, busyId, kinds, onAct, t, fmtTime]);

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
      searchable={!supportsServer}
      searchPlaceholder={t("searchPage")}
      enableSavedViews
      enableCsv
      csvFilename="jobs"
      pageSize={pageSize}
      pageSizeOptions={pageSizeOptions}
      serverPagination={serverPagination}
      manualSorting={supportsServer}
      sorting={sorting}
      onSortingChange={handleSortingChange}
      stickyHeader
      maxBodyHeight="calc(100vh - 360px)"
      tableMinWidth={980}
      emptyTitle={t("emptyTitle")}
      emptyDescription={t("emptyDesc")}
      onRowClick={(j) => onRowClick(j.id)}
      toolbarExtra={
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          {/* Server-side search (id prefix / kind / created_by). In the fallback (no server total)
              the DataTable's own client search box is used instead (searchable above). */}
          {supportsServer && (
            <input value={q} onChange={e => onQChange(e.target.value)} aria-label={t("searchJobs")}
                   placeholder={t("searchPlaceholder")} style={{ ...inputStyle, minWidth: 180 }} />
          )}
          <select value={kind} onChange={e => onKindChange(e.target.value)} aria-label={t("colKind")}
                  style={{ ...inputStyle, maxWidth: 220 }}>
            <option value="">{t("allKinds")}</option>
            {kinds.map(k => <option key={k.kind} value={k.kind}>{k.kind}</option>)}
          </select>
          <select value={status} onChange={e => onStatusChange(e.target.value)} aria-label={t("colStatus")}
                  style={inputStyle}>
            <option value="">{t("allStatuses")}</option>
            {STATUSES.map(s => <option key={s} value={s}>{s}</option>)}
          </select>
          {/* Created by: free text with a datalist of the distinct values on the current page. */}
          <input value={createdBy} onChange={e => onCreatedByChange(e.target.value)} aria-label={t("createdBy")}
                 list="jobs-created-by" placeholder={t("createdByPlaceholder")} style={{ ...inputStyle, maxWidth: 180 }} />
          <datalist id="jobs-created-by">
            {createdByOptions.map(o => <option key={o} value={o} />)}
          </datalist>
          <label style={{ fontSize: 11, color: K.muted, display: "inline-flex", alignItems: "center", gap: 4 }}>
            {t("since")}
            <input type="date" value={since} onChange={e => onSinceChange(e.target.value)} aria-label={t("createdSince")} style={inputStyle} />
          </label>
          <label style={{ fontSize: 11, color: K.muted, display: "inline-flex", alignItems: "center", gap: 4 }}>
            {t("until")}
            <input type="date" value={until} onChange={e => onUntilChange(e.target.value)} aria-label={t("createdUntil")} style={inputStyle} />
          </label>
        </div>
      }
    />
  );
}
