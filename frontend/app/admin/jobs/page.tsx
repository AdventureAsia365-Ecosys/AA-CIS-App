"use client";
// app/admin/jobs/page.tsx — AA-721 Jobs page on the UI kit + react-query, AA-755 server
// pagination/sort/filters + tabs.
//
// What it shows: the durable job queue (shared.job, AA-650) — what is queued/running, what finished,
// what it cost — in two tabs: "Jobs" (a server-paged/sorted/filtered table) and "Workers & queue"
// (the worker panel for the separate aa-cis-dev-worker ECS service + the registered-kind table).
// API: /admin/job-runner/* (NOT /admin/jobs, the older AA-223 run-tour poll). Built on the kit
// DataTable / StatusBadge / Drawer / PageHeader / Tabs and react-query (useQuery + refetchInterval
// for the 10 s poll on the CURRENT page only, useMutation for cancel/retry) — no setInterval, no
// hand-rolled fetch loop.
//
// AA-755 server contract: the list endpoint takes page/page_size/sort/sort_dir and returns a
// `total`/`pagination` block. An older Dev API (e.g. the Vercel preview before the backend ships)
// returns no `total` and ignores the new params — the page still renders then: it falls back to
// `jobs.length` as the total, keeps the server default order, and the column sort is silently not
// applied (supportsServer=false). The smoke stays green on the preview.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { useMemo, useRef, useState } from "react";
import { A, mono, Btn, LoadingScreen } from "../_components/adminUi";
import {
  PageHeader,
  StatusBadge,
  Tabs,
  ToastProvider,
  apiGet,
  apiSend,
  useToast,
} from "../../_kit";
import JobDrawer from "./JobDrawer";
import JobsTable from "./JobsTable";
import QueuePanel from "./QueuePanel";
import {
  STATUSES, fmtSeconds, stepProgress,
  type Count, type Job, type Kind, type WorkerHealthResp,
} from "./jobsShared";

const REFRESH_MS = 10_000;
const DEFAULT_PAGE_SIZE = 25;

interface Pagination { page: number; page_size: number; total: number; pages: number }
interface JobsResp { jobs: Job[]; total?: number; pagination?: Pagination }
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
  const [kind, setKindRaw] = useState(() => readDeepLinks().kind);
  const [status, setStatusRaw] = useState(() => readDeepLinks().status);
  const [tourId, setTourId] = useState(() => readDeepLinks().tourId);
  const [open, setOpen] = useState<string | null>(() => readDeepLinks().job);

  // AA-755 — server pagination + sort state. A filter/sort/page-size change resets to page 0 so the
  // user never lands past the end of a smaller result.
  const [tab, setTab] = useState<"jobs" | "workers">("jobs");
  const [page, setPage] = useState(0); // 0-based
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
  const [sort, setSort] = useState("created_at");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");

  const setKind = (k: string) => { setKindRaw(k); setPage(0); };
  const setStatus = (s: string) => { setStatusRaw(s); setPage(0); };

  // First sample per (job, step) for the ETA rate. Tracked in the queryFn (which runs outside
  // render, so a ref is safe there) and the resulting ETA map is returned as part of the query
  // data — the render path never touches the ref (React Compiler: no refs/impure calls in render).
  const samples = useRef<Map<string, Sample>>(new Map());

  // ── Query: jobs (current page) + summary + workers, polled together every 10 s. The query key
  // carries page/pageSize/sort/sortDir so paging or re-sorting re-fetches; the poll stays on the
  // CURRENT page + filters only.
  const qs = new URLSearchParams({ page: String(page + 1), page_size: String(pageSize), sort, sort_dir: sortDir });
  if (kind) qs.set("kind", kind);
  if (status) qs.set("status", status);
  if (tourId) qs.set("tour_id", tourId);

  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ["jobs", kind, status, tourId, page, pageSize, sort, sortDir],
    queryFn: async () => {
      const [j, s, w] = await Promise.all([
        apiGet<JobsResp>(`/api/admin/job-runner/jobs?${qs}`, { admin: true }),
        apiGet<SummaryResp>(`/api/admin/job-runner/summary`, { admin: true }),
        // The worker panel is optional: an older API (before AA-687) has no /workers.
        apiGet<WorkerHealthResp>(`/api/admin/job-runner/workers`, { admin: true }).catch(() => null),
      ]);
      const jobs = j.jobs ?? [];
      // AA-755 fallback: an older API returns no `total` — use the page length and mark the page as
      // not server-driven (no server sort, no multi-page pager).
      const supportsServer = typeof j.total === "number";
      const total = supportsServer ? (j.total as number) : jobs.length;
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
        jobs, etas, now: t, total, supportsServer,
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
  const total = data?.total ?? jobs.length;
  const supportsServer = data?.supportsServer ?? false;

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
  const busyId = actM.isPending ? (actM.variables?.job.id ?? null) : null;

  const expected = (k: string) => kinds.find(x => x.kind === k)?.expected_seconds;

  // Status filter chips (counts across the 30-day summary, respecting the kind filter).
  const byStatus = STATUSES.map(s => ({
    status: s,
    n: counts.filter(c => c.status === s && (!kind || c.kind === kind)).reduce((a, c) => a + c.n, 0),
  }));
  const cost30d = counts.filter(c => !kind || c.kind === kind).reduce((a, c) => a + c.cost_usd, 0);
  const openJob = jobs.find(j => j.id === open);

  // First load only: show the full-page spinner. Later refetches keep the UI visible.
  if (isLoading && !data) return <LoadingScreen msg="Loading jobs..." />;

  return (
    <main className="aa-admin-main" style={{ flex: 1, padding: "32px 36px 56px", minWidth: 0, minHeight: 0, overflowY: "auto" }}>
      <PageHeader
        title="Jobs"
        description="Background work that survives deploys — queued, running, finished. Refreshes every 10 s."
        actions={
          <>
            {tourId && (
              <button onClick={() => { setTourId(""); setPage(0); }} title="Clear the tour filter"
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

      <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap", marginBottom: 16 }}>
        <Tabs
          tabs={[{ key: "jobs", label: "Jobs" }, { key: "workers", label: "Workers & queue" }]}
          active={tab}
          onChange={(k) => setTab(k as "jobs" | "workers")}
        />
        <div style={{ marginLeft: "auto", fontSize: 12, color: A.muted, alignSelf: "center" }}>
          Cost, last 30 days: <span style={{ fontFamily: mono, color: A.ink }}>${cost30d.toFixed(4)}</span>
        </div>
      </div>

      {tab === "jobs" ? (
        <>
          {/* Status filter chips */}
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 16 }}>
            {byStatus.map(s => (
              <button key={s.status} onClick={() => setStatus(status === s.status ? "" : s.status)}
                      style={{ cursor: "pointer", border: `1px solid ${status === s.status ? A.accent : A.line}`, background: A.card, borderRadius: 8, padding: "8px 12px", display: "flex", gap: 8, alignItems: "center" }}>
                <StatusBadge status={s.status} />
                <span style={{ fontFamily: mono, fontSize: 13, color: A.ink }}>{s.n}</span>
              </button>
            ))}
          </div>

          <JobsTable
            jobs={jobs}
            kinds={kinds}
            etas={etas}
            now={now}
            maxReleases={maxReleases}
            busyId={busyId}
            page={page}
            pageSize={pageSize}
            total={total}
            supportsServer={supportsServer}
            onPageChange={(pi, ps) => { setPage(pi); if (ps !== pageSize) { setPageSize(ps); setPage(0); } }}
            sort={sort}
            sortDir={sortDir}
            onSortChange={(s, d) => { setSort(s); setSortDir(d); setPage(0); }}
            kind={kind}
            status={status}
            onKindChange={setKind}
            onStatusChange={setStatus}
            onRowClick={setOpen}
            onAct={act}
            loading={isLoading && !data}
            error={isError ? (error instanceof Error ? error.message : "Could not load jobs") : null}
            onRetry={() => refetch()}
          />
        </>
      ) : (
        <QueuePanel
          health={health}
          kinds={kinds}
          counts={counts}
          now={now}
          onPick={(k, s) => { setKind(k); setStatus(s); setTab("jobs"); }}
        />
      )}

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
