"use client";
// app/admin/jobs/page.tsx — AA-721 Jobs page on the UI kit + react-query.
//
// What it shows: the durable job queue (shared.job, AA-650) — what is queued/running, what finished,
// what it cost — plus a worker panel (the jobs run in the separate aa-cis-dev-worker ECS service,
// not inside the API) and a registry of every job kind. API: /admin/job-runner/* (NOT /admin/jobs,
// which is the older AA-223 run-tour poll). Built on the kit DataTable / StatusBadge / Drawer /
// PageHeader and react-query (useQuery + refetchInterval for the live poll, useMutation for
// cancel/retry) — no setInterval, no hand-rolled fetch loop.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { useMemo, useRef, useState } from "react";
import { A, mono, Btn, LoadingScreen } from "../_components/adminUi";
import {
  Badge,
  type ColumnDef,
  DataTable,
  PageHeader,
  StatusBadge,
  ToastProvider,
  apiGet,
  apiSend,
  useToast,
} from "../../_kit";
import JobDrawer from "./JobDrawer";
import JobKinds from "./JobKinds";
import WorkerHealth from "./WorkerHealth";
import {
  STATUSES, fmtSeconds, fmtTime, releases, runSeconds, stepProgress, usd,
  type Count, type Job, type Kind, type WorkerHealthResp,
} from "./jobsShared";

const REFRESH_MS = 10_000;

interface JobsResp { jobs: Job[] }
interface SummaryResp { counts: Count[]; kinds: Kind[]; max_releases?: number }

/** First sample seen per (job, step): the ETA is (total - done) / observed rate since then. */
type Sample = { step: string; done: number; t: number };

function readDeepLinks() {
  if (typeof window === "undefined") return { kind: "", status: "", tourId: "", job: null as string | null };
  const q = new URLSearchParams(window.location.search);
  return {
    kind: q.get("kind") ?? "",
    status: q.get("status") ?? "",
    tourId: q.get("tour_id") ?? "",
    job: q.get("job"),
  };
}

function JobsPageInner() {
  const toast = useToast();
  const qc = useQueryClient();

  // Deep links (?job= / ?kind= / ?status= / ?tour_id=) read once as lazy initial state — not in an
  // effect, which the React Compiler forbids (AGENTS.md / AA-739 pattern). Null-safe for SSR.
  const [kind, setKind] = useState(() => readDeepLinks().kind);
  const [status, setStatus] = useState(() => readDeepLinks().status);
  const [tourId, setTourId] = useState(() => readDeepLinks().tourId);
  const [open, setOpen] = useState<string | null>(() => readDeepLinks().job);

  // First sample per (job, step) for the ETA rate. Tracked in the queryFn (which runs outside
  // render, so a ref is safe there) and the resulting ETA map is returned as part of the query
  // data — the render path never touches the ref (React Compiler: no refs/impure calls in render).
  const samples = useRef<Map<string, Sample>>(new Map());

  // ── Query: jobs + summary + workers, polled together every 10 s (refetchInterval, no setInterval).
  const qs = new URLSearchParams({ limit: "100" });
  if (kind) qs.set("kind", kind);
  if (status) qs.set("status", status);
  if (tourId) qs.set("tour_id", tourId);

  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ["jobs", kind, status, tourId],
    queryFn: async () => {
      const [j, s, w] = await Promise.all([
        apiGet<JobsResp>(`/api/admin/job-runner/jobs?${qs}`, { admin: true }),
        apiGet<SummaryResp>(`/api/admin/job-runner/summary`, { admin: true }),
        // The worker panel is optional: an older API (before AA-687) has no /workers.
        apiGet<WorkerHealthResp>(`/api/admin/job-runner/workers`, { admin: true }).catch(() => null),
      ]);
      const jobs = j.jobs ?? [];
      const t = Date.now();
      // Update the first-seen sample per (job, step) and compute the ETA for each running job.
      const etas: Record<string, string | null> = {};
      for (const job of jobs) {
        const p = stepProgress(job);
        const prev = samples.current.get(job.id);
        if (p && (!prev || prev.step !== p.step || p.done < prev.done)) {
          samples.current.set(job.id, { step: p.step, done: p.done, t });
        }
        const first = samples.current.get(job.id);
        if (!p || !first || first.step !== p.step || p.done <= first.done) {
          etas[job.id] = null;
        } else {
          const rate = (p.done - first.done) / ((t - first.t) / 1000);
          etas[job.id] = rate > 0 ? fmtSeconds((p.total - p.done) / rate) : null;
        }
      }
      return {
        jobs, etas, now: t,
        counts: s.counts ?? [], kinds: s.kinds ?? [],
        maxReleases: typeof s.max_releases === "number" ? s.max_releases : 3,
        workers: w as WorkerHealthResp | null,
      };
    },
    placeholderData: (prev) => prev,
    refetchInterval: REFRESH_MS,
  });

  const jobs = useMemo(() => data?.jobs ?? [], [data]);
  const counts = data?.counts ?? [];
  const kinds = useMemo(() => data?.kinds ?? [], [data]);
  const maxReleases = data?.maxReleases ?? 3;
  const health = data?.workers ?? null;
  const etas = useMemo(() => data?.etas ?? {}, [data]);
  const now = data?.now ?? 0;

  // ── cancel / retry via useMutation ──
  const actM = useMutation({
    mutationFn: ({ job, action }: { job: Job; action: "cancel" | "retry" }) =>
      apiSend(`/api/admin/job-runner/jobs/${job.id}/${action}`, { method: "POST", admin: true }),
    onSuccess: (_r, { action }) => {
      toast.success(action === "cancel" ? "Cancel requested" : "Re-queued");
      qc.invalidateQueries({ queryKey: ["jobs"] });
      qc.invalidateQueries({ queryKey: ["job", open] });
    },
    onError: (e, { action }) =>
      toast.error(`${action === "cancel" ? "Cancel" : "Retry"} failed: ${e instanceof Error ? e.message : e}`),
  });

  const act = (job: Job, action: "cancel" | "retry") => {
    const verb = action === "cancel" ? "Cancel" : "Retry";
    if (!window.confirm(`${verb} ${job.kind} job ${job.id.slice(0, 8)}?`)) return;
    actM.mutate({ job, action });
  };
  const busyId = actM.isPending ? actM.variables?.job.id : null;

  const expected = (k: string) => kinds.find(x => x.kind === k)?.expected_seconds;

  // Status filter chips (counts across the 30-day summary, respecting the kind filter).
  const byStatus = STATUSES.map(s => ({
    status: s,
    n: counts.filter(c => c.status === s && (!kind || c.kind === kind)).reduce((a, c) => a + c.n, 0),
  }));
  const cost30d = counts.filter(c => !kind || c.kind === kind).reduce((a, c) => a + c.cost_usd, 0);
  const openJob = jobs.find(j => j.id === open);

  // ── Columns (DataTable gives phone cards for free below 768px, AA-752) ──
  const columns = useMemo<ColumnDef<Job, unknown>[]>(() => [
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
      id: "created",
      header: "Created",
      cell: (c) => <span style={{ color: A.muted, whiteSpace: "nowrap" }}>{fmtTime(c.row.original.created_at)}</span>,
    },
    {
      accessorKey: "created_by",
      header: "By",
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
            {canCancel && <Btn size="sm" variant="danger" disabled={busyId === j.id} onClick={() => act(j, "cancel")}>Cancel</Btn>}
            {canRetry && <Btn size="sm" disabled={busyId === j.id} onClick={() => act(j, "retry")}>Retry</Btn>}
          </div>
        );
      },
    },
  ], [now, etas, maxReleases, busyId, kinds]);

  // First load only: show the full-page spinner. Later refetches keep the table visible.
  if (isLoading && !data) return <LoadingScreen msg="Loading jobs..." />;

  return (
    <main className="aa-admin-main" style={{ flex: 1, padding: "32px 36px 56px", minWidth: 0, minHeight: 0, overflowY: "auto" }}>
      <PageHeader
        title="Jobs"
        description="Background work that survives deploys — queued, running, finished. Refreshes every 10 s."
        actions={
          <>
            {tourId && (
              <button onClick={() => setTourId("")} title="Clear the tour filter"
                      style={{ fontSize: 12, padding: "6px 8px", border: `1px solid ${A.accent}`, borderRadius: 6, background: A.card, color: A.ink3, cursor: "pointer", fontFamily: mono }}>
                tour {tourId.slice(0, 8)} ×
              </button>
            )}
            <Btn size="sm" onClick={() => refetch()}>
              <RefreshCw size={12} className={isFetching ? "spin" : undefined} style={{ marginRight: 4 }} />Refresh
            </Btn>
          </>
        }
      />
      <style>{`@keyframes spin{to{transform:rotate(360deg)}}.spin{animation:spin .8s linear infinite}`}</style>

      {isError && (
        <div style={{ marginBottom: 14, padding: "10px 12px", borderRadius: 8, background: A.redSoft, color: A.red, border: `1px solid ${A.redBorder}`, fontSize: 12.5 }}>
          Could not load jobs ({error instanceof Error ? error.message : String(error)})
        </div>
      )}

      <WorkerHealth health={health} kinds={kinds} now={now} />

      {/* Status filter chips */}
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 16 }}>
        {byStatus.map(s => (
          <button key={s.status} onClick={() => setStatus(status === s.status ? "" : s.status)}
                  style={{ cursor: "pointer", border: `1px solid ${status === s.status ? A.accent : A.line}`, background: A.card, borderRadius: 8, padding: "8px 12px", display: "flex", gap: 8, alignItems: "center" }}>
            <StatusBadge status={s.status} />
            <span style={{ fontFamily: mono, fontSize: 13, color: A.ink }}>{s.n}</span>
          </button>
        ))}
        <div style={{ marginLeft: "auto", fontSize: 12, color: A.muted, alignSelf: "center" }}>
          Cost, last 30 days: <span style={{ fontFamily: mono, color: A.ink }}>${cost30d.toFixed(4)}</span>
        </div>
      </div>

      <DataTable<Job>
        data={jobs}
        columns={columns}
        tableId="admin-jobs"
        getRowId={(j) => j.id}
        loading={isLoading && !data}
        error={isError ? (error instanceof Error ? error.message : "Could not load jobs") : null}
        onRetry={() => refetch()}
        searchable
        searchPlaceholder="Search jobs…"
        enableSavedViews
        enableCsv
        csvFilename="jobs"
        pageSize={25}
        pageSizeOptions={[25, 50, 100]}
        tableMinWidth={980}
        emptyTitle="No jobs match these filters yet"
        emptyDescription="Background work shows here as it is enqueued."
        onRowClick={(j) => setOpen(j.id)}
        toolbarExtra={
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <select value={kind} onChange={e => setKind(e.target.value)} aria-label="Kind"
                    style={{ fontSize: 12, padding: "6px 8px", border: `1px solid ${A.line}`, borderRadius: 6, background: A.card, color: A.ink3, maxWidth: 220, minWidth: 0 }}>
              <option value="">All kinds</option>
              {kinds.map(k => <option key={k.kind} value={k.kind}>{k.kind}</option>)}
            </select>
            <select value={status} onChange={e => setStatus(e.target.value)} aria-label="Status"
                    style={{ fontSize: 12, padding: "6px 8px", border: `1px solid ${A.line}`, borderRadius: 6, background: A.card, color: A.ink3 }}>
              <option value="">All statuses</option>
              {STATUSES.map(s => <option key={s} value={s}>{s}</option>)}
            </select>
          </div>
        }
      />

      <JobKinds kinds={kinds} counts={counts} onPick={(k, s) => { setKind(k); setStatus(s); }} />

      {open && (
        <JobDrawer
          jobId={open}
          refetchMs={REFRESH_MS}
          expectedSeconds={openJob ? expected(openJob.kind) : undefined}
          maxReleases={maxReleases}
          eta={etas[open] ?? null}
          onClose={() => setOpen(null)}
          onAct={act}
          busy={busyId === open}
        />
      )}
    </main>
  );
}

export default function JobsPage() {
  return (
    <ToastProvider>
      <JobsPageInner />
    </ToastProvider>
  );
}
