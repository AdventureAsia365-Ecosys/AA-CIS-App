"use client";
// app/admin/cost-ops/DfsRunsPanel.tsx — AA-665 C2: Cost Operations › DataForSEO › Runs: what triggered each DataForSEO
// run. API: GET /api/admin/dfs-usage/runs?{window}[&infer_unlinked=true] (backend api/services/cost_ops.py dfs_runs;
// spend.read = admin). Rows carry shared.dfs_call_log.job_id since CIS 293; calls made outside a job (and every older
// row) are one "No job" row. "Match calls without a job" asks the backend for a best-effort match by the jobs' run
// windows (windows of at most 31 days); calls inside a recorded cost incident with no job running are matched to the
// incident. A recorded incident overlapping the window is shown above the runs (IncidentCard).
// AA-994: text in the UI language (panels.messages.ts); job kinds, statuses, scopes and endpoints stay as sent.

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useMemo, useState } from "react";
import {
  Badge, Button, DataTable, ErrorState, K, SkeletonBar, apiGet, mono, useFormat, useT, type ColumnDef, type Tone,
} from "../../_kit";
import { PanelTitle, Stat, int, noteStyle, numStyle, panelStyle, statGrid, usd } from "./costOpsUi";
import { PANELS } from "./panels.messages";
import IncidentCard, { type IncidentView } from "./IncidentCard";

interface JobView {
  kind: string | null;
  status: string | null;
  created_by: string | null;
  scope: Record<string, string>;
  job_reported_cost_usd: number;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
}
interface Run {
  job_id: string | null;
  attribution: "job" | "none";
  job_found: boolean;
  calls: number;
  live_calls: number;
  cache_hits: number;
  keywords: number;
  dfs_cost_usd: number;
  llm_cost_usd: number;
  endpoints: string[];
  first_call_at: string | null;
  last_call_at: string | null;
  job: JobView | null;
}
interface Inferred {
  job_id: string | null;
  attribution: "window" | "ambiguous" | "incident" | "none";
  candidate_kinds: string[];
  incident_id: string | null;
  incident_title: string | null;
  calls: number;
  live_calls: number;
  dfs_cost_usd: number;
  first_call_at: string | null;
  last_call_at: string | null;
  job: JobView | null;
}
interface RunsResp {
  summary: { calls: number; dfs_cost_usd: number; linked_calls: number; linked_cost_usd: number; jobs: number };
  runs: Run[];
  truncated: boolean;
  inferred: Inferred[] | null;
  incidents?: IncidentView[];
}

const STATUS_TONE: Record<string, Tone> = {
  succeeded: "success", failed: "danger", stopped_budget: "warning", cancelled: "neutral", running: "info",
  queued: "neutral",
};
const ATTRIBUTION: Record<
  Inferred["attribution"], { tone: Tone; label: "attrWindow" | "attrAmbiguous" | "attrIncident" | "attrNone" }
> = {
  window: { tone: "info", label: "attrWindow" },
  ambiguous: { tone: "warning", label: "attrAmbiguous" },
  incident: { tone: "danger", label: "attrIncident" },
  none: { tone: "neutral", label: "attrNone" },
};

function JobCell({ jobId, job, fallback }: { jobId: string | null; job: JobView | null; fallback: string }) {
  const t = useT(PANELS);
  if (!jobId) return <span style={{ color: K.muted }}>{fallback}</span>;
  return (
    <div style={{ minWidth: 0 }}>
      <div style={{ fontWeight: 600 }}>{job?.kind ?? t("jobGone")}</div>
      <Link href={`/admin/jobs?job=${jobId}`} style={{ fontFamily: mono, fontSize: 11.5, color: K.accentDeep }}>
        {jobId.slice(0, 8)}
      </Link>
    </div>
  );
}

function scopeText(job: JobView | null): string {
  if (!job) return "—";
  const parts = Object.entries(job.scope).map(([k, v]) => `${k}: ${v}`);
  return parts.length ? parts.join(" · ") : "—";
}

function when(first: string | null, last: string | null, formatDateTime: (d: string | null) => string): string {
  if (!first) return "—";
  return first === last ? formatDateTime(first) : `${formatDateTime(first)} → ${formatDateTime(last)}`;
}

// The backend matches calls without a job only over windows of at most this many days (422 beyond).
const INFER_MAX_DAYS = 31;

export default function DfsRunsPanel({ rangeQs, rangeLabel, windowDays }: {
  rangeQs: string; rangeLabel: string; windowDays: number;
}) {
  const t = useT(PANELS);
  const { formatDateTime } = useFormat();
  const [infer, setInfer] = useState(false);
  const canInfer = windowDays <= INFER_MAX_DAYS;
  const inferOn = infer && canInfer;
  const qs = inferOn ? `${rangeQs}&infer_unlinked=true` : rangeQs;
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["cost-ops-dfs-runs", qs],
    queryFn: () => apiGet<RunsResp>(`/api/admin/dfs-usage/runs?${qs}`, { admin: true }),
  });

  const columns = useMemo<ColumnDef<Run, unknown>[]>(() => [
    {
      id: "job", header: t("colJob"),
      cell: (c) => <JobCell jobId={c.row.original.job_id} job={c.row.original.job} fallback={t("noJob")} />,
    },
    { id: "by", header: t("colStartedBy"), cell: (c) => c.row.original.job?.created_by ?? "—" },
    {
      id: "scope", header: t("colScope"),
      cell: (c) => <span style={{ fontSize: 12, color: K.ink3 }}>{scopeText(c.row.original.job)}</span>,
    },
    {
      id: "status", header: t("colStatus"),
      cell: (c) => {
        const j = c.row.original.job;
        if (!j?.status) return "—";
        return (
          <span title={j.error ?? undefined}>
            <Badge tone={STATUS_TONE[j.status] ?? "neutral"}>{j.status}</Badge>
          </span>
        );
      },
    },
    {
      accessorKey: "dfs_cost_usd", header: "DataForSEO", meta: { align: "right" },
      cell: (c) => <span style={{ ...numStyle, fontWeight: 600 }}>{usd(c.row.original.dfs_cost_usd, 4)}</span>,
    },
    {
      accessorKey: "llm_cost_usd", header: t("colLlmSameJob"), meta: { align: "right" },
      cell: (c) => <span style={numStyle}>{c.row.original.job_id ? usd(c.row.original.llm_cost_usd, 4) : "—"}</span>,
    },
    {
      accessorKey: "calls", header: t("colCallsLiveCache"), meta: { align: "right" },
      cell: (c) => {
        const r = c.row.original;
        return <span style={numStyle}>{int(r.calls)} ({int(r.live_calls)} / {int(r.cache_hits)})</span>;
      },
    },
    {
      id: "endpoints", header: t("colEndpoints"),
      cell: (c) => <span style={{ fontFamily: mono, fontSize: 11.5 }}>{c.row.original.endpoints.join(", ")}</span>,
    },
    {
      id: "when", header: t("colCallsMade"),
      cell: (c) => when(c.row.original.first_call_at, c.row.original.last_call_at, formatDateTime),
    },
  ], [t, formatDateTime]);

  const inferredColumns = useMemo<ColumnDef<Inferred, unknown>[]>(() => [
    {
      id: "attribution", header: t("colMatch"),
      cell: (c) => {
        const m = ATTRIBUTION[c.row.original.attribution];
        return <Badge tone={m.tone}>{t(m.label)}</Badge>;
      },
    },
    {
      id: "job", header: t("colJob"),
      cell: (c) => {
        const r = c.row.original;
        if (r.attribution === "ambiguous") {
          return <span style={{ fontSize: 12, color: K.ink3 }}>{r.candidate_kinds.join(", ")}</span>;
        }
        if (r.attribution === "incident") {
          return <span style={{ fontSize: 12, color: K.ink }}>{r.incident_title ?? r.incident_id}</span>;
        }
        return <JobCell jobId={r.job_id} job={r.job} fallback="—" />;
      },
    },
    {
      id: "scope", header: t("colScope"),
      cell: (c) => <span style={{ fontSize: 12, color: K.ink3 }}>{scopeText(c.row.original.job)}</span>,
    },
    {
      accessorKey: "dfs_cost_usd", header: "DataForSEO", meta: { align: "right" },
      cell: (c) => <span style={{ ...numStyle, fontWeight: 600 }}>{usd(c.row.original.dfs_cost_usd, 4)}</span>,
    },
    {
      accessorKey: "calls", header: t("colCallsLive"), meta: { align: "right" },
      cell: (c) => <span style={numStyle}>{int(c.row.original.calls)} ({int(c.row.original.live_calls)})</span>,
    },
    {
      id: "when", header: t("colCallsMade"),
      cell: (c) => when(c.row.original.first_call_at, c.row.original.last_call_at, formatDateTime),
    },
  ], [t, formatDateTime]);

  return (
    <div style={panelStyle} data-testid="cost-ops-dfs-runs">
      <PanelTitle aside={
        <Button size="sm" variant={inferOn ? "primary" : "secondary"} onClick={() => setInfer((v) => !v)}
                disabled={!canInfer}
                title={canInfer ? undefined : t("inferWindowTitle", INFER_MAX_DAYS)}>
          {inferOn ? t("hideMatched") : t("matchCalls")}
        </Button>
      }>
        {t("runsTitle")}
      </PanelTitle>
      {isLoading && <div style={{ display: "flex", flexDirection: "column", gap: 8 }}><SkeletonBar /><SkeletonBar w="80%" /></div>}
      {isError && <ErrorState message={t("runsLoadError", (error as Error)?.message ?? "")} onRetry={() => refetch()} />}
      {data && (
        <>
          {(data.incidents ?? []).map((inc) => <IncidentCard key={inc.id} incident={inc} />)}
          <div style={statGrid}>
            <Stat label={t("dfsSpend")} value={usd(data.summary.dfs_cost_usd)} sub={rangeLabel} />
            <Stat label={t("linkedToJob")} value={usd(data.summary.linked_cost_usd)}
                  sub={t("linkedOf", int(data.summary.linked_calls), int(data.summary.calls))} />
            <Stat label={t("jobs")} value={int(data.summary.jobs)} />
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)" }}>
            <DataTable<Run>
              tableId="cost-ops-dfs-runs"
              data={data.runs}
              columns={columns}
              getRowId={(r) => r.job_id ?? "none"}
              pageSize={20}
              enableCsv
              csvFilename="dfs-runs.csv"
              tableMinWidth={1100}
              compact
              emptyTitle={t("noDfsCalls")}
              emptyDescription={t("nothingLogged", rangeLabel)}
            />
          </div>
          {data.truncated && <div style={{ ...noteStyle, marginTop: 6 }}>{t("top200")}</div>}
          {inferOn && data.inferred && (
            <div style={{ marginTop: 16 }}>
              <PanelTitle>{t("matchedTitle")}</PanelTitle>
              <div style={{ ...noteStyle, marginBottom: 8 }}>{t("matchedNote")}</div>
              <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)" }}>
                <DataTable<Inferred>
                  tableId="cost-ops-dfs-inferred"
                  data={data.inferred}
                  columns={inferredColumns}
                  getRowId={(r) => `${r.attribution}-${r.job_id ?? r.incident_id ?? r.candidate_kinds.join(",")}`}
                  pageSize={20}
                  tableMinWidth={820}
                  compact
                  emptyTitle={t("everyCallHasJob")}
                />
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
