"use client";
// app/admin/jobs/page.tsx — AA-721 Jobs page on the UI kit + react-query, AA-755 server
// pagination/sort/filters + tabs.
//
// What it shows: the durable job queue (shared.job, AA-650) — what is queued/running, what finished,
// what it cost — in three tabs: "Jobs" (a server-paged/sorted/filtered table), "Queue & workers"
// (the worker panel for the separate aa-cis-dev-worker ECS service + the queue-by-kind view) and
// "Job kinds" (the registered-kind table). API: /admin/job-runner/* (NOT /admin/jobs, the older
// AA-223 run-tour poll). Built on the kit DataTable / StatusBadge / Drawer / PageHeader / Tabs and
// react-query (useQuery + refetchInterval for the 10 s poll on the CURRENT page only, useMutation
// for cancel/retry) — no setInterval, no hand-rolled fetch loop.
//
// URL state (AA-755): tab, kind, status, created_by, since, until, q, sort, dir, page, page_size are
// all kept in the URL via history.replaceState (same approach as the older deep links), so a view is
// shareable and survives a refresh.
//
// Server contract: the list endpoint takes page/page_size/sort/sort_dir + since/until/created_by/q
// and returns a `total`/`pagination` block that counts ALL filter-matching rows. An older Dev API
// (e.g. the Vercel preview before the backend ships) returns no `total` and ignores the new params —
// the page still renders then: it falls back to `jobs.length` as the total, keeps the server default
// order, applies the search client-side, and the column sort is silently not applied
// (supportsServer=false). The smoke stays green on the preview.

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
import JobKinds from "./JobKinds";
import {
  STATUSES, fmtSeconds, stepProgress,
  type Count, type Job, type Kind, type WorkerHealthResp,
} from "./jobsShared";

const REFRESH_MS = 10_000;
const PAGE_SIZES = [50, 100, 200];
const DEFAULT_PAGE_SIZE = 100;
type TabKey = "jobs" | "queue" | "kinds";

interface Pagination { page: number; page_size: number; total: number; pages: number }
interface JobsResp { jobs: Job[]; total?: number; pagination?: Pagination }
interface SummaryResp { counts: Count[]; kinds: Kind[]; max_releases?: number }

/** First sample seen per (job, step): the ETA is (total - done) / observed rate since then. */
type Sample = { step: string; done: number; t: number };

// ── URL-synced view state ───────────────────────────────────────────────────────────────────────
// All of it lives in one object so a single `update()` can merge, reset the page on a filter change
// and write the URL in one place (no per-field setter, no effect that calls setState).
interface View {
  tab: TabKey;
  kind: string;
  status: string;
  createdBy: string;
  since: string;      // ISO date (yyyy-mm-dd) or "" — fed straight to the API `since`
  until: string;      // ISO date — API `until`
  q: string;
  tourId: string;
  sort: string;
  dir: "asc" | "desc";
  page: number;       // 0-based
  pageSize: number;
  job: string | null; // open drawer
}

const EMPTY_VIEW: View = {
  tab: "jobs", kind: "", status: "", createdBy: "", since: "", until: "", q: "", tourId: "",
  sort: "created_at", dir: "desc", page: 0, pageSize: DEFAULT_PAGE_SIZE, job: null,
};

/** Read the view once from the URL as lazy initial state (not in an effect — the React Compiler
 * forbids setState in effects; AGENTS.md / AA-739 pattern). Null-safe for SSR. */
function readView(): View {
  if (typeof window === "undefined") return { ...EMPTY_VIEW };
  const p = new URLSearchParams(window.location.search);
  const tabRaw = p.get("tab");
  const tab: TabKey = tabRaw === "queue" || tabRaw === "kinds" ? tabRaw : "jobs";
  const dir = p.get("dir") === "asc" ? "asc" : "desc";
  const ps = Number(p.get("page_size"));
  const page = Math.max(0, (Number(p.get("page")) || 1) - 1);
  return {
    tab,
    kind: p.get("kind") ?? "",
    status: p.get("status") ?? "",
    createdBy: p.get("created_by") ?? "",
    since: p.get("since") ?? "",
    until: p.get("until") ?? "",
    q: p.get("q") ?? "",
    tourId: p.get("tour_id") ?? "",
    sort: p.get("sort") ?? "created_at",
    dir,
    page: Number.isFinite(page) ? page : 0,
    pageSize: PAGE_SIZES.includes(ps) ? ps : DEFAULT_PAGE_SIZE,
    job: p.get("job"),
  };
}

/** Write only the non-default fields to the URL (replaceState — no history spam, matches the old
 * deep-link behaviour). `tour_id` and `job` keep their original param names. */
function writeView(v: View) {
  if (typeof window === "undefined") return;
  const p = new URLSearchParams();
  if (v.tab !== "jobs") p.set("tab", v.tab);
  if (v.kind) p.set("kind", v.kind);
  if (v.status) p.set("status", v.status);
  if (v.createdBy) p.set("created_by", v.createdBy);
  if (v.since) p.set("since", v.since);
  if (v.until) p.set("until", v.until);
  if (v.q) p.set("q", v.q);
  if (v.tourId) p.set("tour_id", v.tourId);
  if (v.sort !== "created_at") p.set("sort", v.sort);
  if (v.dir !== "desc") p.set("dir", v.dir);
  if (v.page > 0) p.set("page", String(v.page + 1));
  if (v.pageSize !== DEFAULT_PAGE_SIZE) p.set("page_size", String(v.pageSize));
  if (v.job) p.set("job", v.job);
  const qs = p.toString();
  window.history.replaceState(null, "", qs ? `?${qs}` : window.location.pathname);
}

// Which fields are filters — changing any of them resets the page to 0.
const FILTER_KEYS: (keyof View)[] = ["kind", "status", "createdBy", "since", "until", "q", "tourId"];

function JobsPageInner() {
  const toast = useToast();
  const qc = useQueryClient();

  const [view, setView] = useState<View>(() => readView());

  /** Merge a patch into the view, reset the page when a filter changed, and sync the URL. */
  const update = (patch: Partial<View>) => {
    setView((prev) => {
      const touchesFilter = FILTER_KEYS.some((k) => k in patch && patch[k] !== prev[k]);
      const next: View = { ...prev, ...patch };
      if (touchesFilter && !("page" in patch)) next.page = 0;
      writeView(next);
      return next;
    });
  };

  const { tab, kind, status, createdBy, since, until, q, tourId, sort, dir, page, pageSize, job: open } = view;

  // First sample per (job, step) for the ETA rate. Tracked in the queryFn (which runs outside
  // render, so a ref is safe there) and the resulting ETA map is returned as part of the query
  // data — the render path never touches the ref (React Compiler: no refs/impure calls in render).
  const samples = useRef<Map<string, Sample>>(new Map());

  // ── Query: jobs (current page) + summary + workers, polled together every 10 s. The query key
  // carries every server-side filter + sort + page so any change re-fetches; the poll stays on the
  // CURRENT page + filters only.
  const qs = new URLSearchParams({ page: String(page + 1), page_size: String(pageSize), sort, sort_dir: dir });
  if (kind) qs.set("kind", kind);
  if (status) qs.set("status", status);
  if (createdBy) qs.set("created_by", createdBy);
  if (since) qs.set("since", since);
  if (until) qs.set("until", until);
  if (q) qs.set("q", q);
  if (tourId) qs.set("tour_id", tourId);

  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ["jobs", kind, status, createdBy, since, until, q, tourId, page, pageSize, sort, dir],
    queryFn: async () => {
      const [j, s, w] = await Promise.all([
        apiGet<JobsResp>(`/api/admin/job-runner/jobs?${qs}`, { admin: true }),
        apiGet<SummaryResp>(`/api/admin/job-runner/summary`, { admin: true }),
        // The worker panel is optional: an older API (before AA-687) has no /workers.
        apiGet<WorkerHealthResp>(`/api/admin/job-runner/workers`, { admin: true }).catch(() => null),
      ]);
      const jobs = j.jobs ?? [];
      // AA-755 fallback: an older API returns no `total` — use the page length and mark the page as
      // not server-driven (no server sort, no multi-page pager, search runs client-side).
      const supportsServer = typeof j.total === "number";
      const total = supportsServer ? (j.total as number) : jobs.length;
      const t = Date.now();
      // Update the first-seen sample per (job, step) and compute the ETA for each running job.
      const etas: Record<string, string | null> = {};
      for (const jb of jobs) {
        const p = stepProgress(jb);
        const prev = samples.current.get(jb.id);
        if (p && (!prev || prev.step !== p.step || p.done < prev.done)) {
          samples.current.set(jb.id, { step: p.step, done: p.done, t });
        }
        const first = samples.current.get(jb.id);
        if (!p || !first || first.step !== p.step || p.done <= first.done) {
          etas[jb.id] = null;
        } else {
          const rate = (p.done - first.done) / ((t - first.t) / 1000);
          etas[jb.id] = rate > 0 ? fmtSeconds((p.total - p.done) / rate) : null;
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

  const act = (jb: Job, action: "cancel" | "retry") => {
    const verb = action === "cancel" ? "Cancel" : "Retry";
    if (!window.confirm(`${verb} ${jb.kind} job ${jb.id.slice(0, 8)}?`)) return;
    actM.mutate({ job: jb, action });
  };
  const busyId = actM.isPending ? (actM.variables?.job.id ?? null) : null;

  const expected = (k: string) => kinds.find(x => x.kind === k)?.expected_seconds;

  // Status filter chips (counts across the 30-day summary, respecting the kind filter).
  const byStatus = STATUSES.map(s => ({
    status: s,
    n: counts.filter(c => c.status === s && (!kind || c.kind === kind)).reduce((a, c) => a + c.n, 0),
  }));
  const cost30d = counts.filter(c => !kind || c.kind === kind).reduce((a, c) => a + c.cost_usd, 0);
  // Distinct created_by values on the current page, for the "Created by" picker (free text also ok).
  const createdByOptions = useMemo(
    () => Array.from(new Set(jobs.map(j => j.created_by).filter((x): x is string => !!x))).sort(),
    [jobs],
  );
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
              <button onClick={() => update({ tourId: "" })} title="Clear the tour filter"
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
          tabs={[
            { key: "jobs", label: "Jobs" },
            { key: "queue", label: "Queue & workers" },
            { key: "kinds", label: "Job kinds" },
          ]}
          active={tab}
          onChange={(k) => update({ tab: k as TabKey })}
        />
        <div style={{ marginLeft: "auto", fontSize: 12, color: A.muted, alignSelf: "center" }}>
          Cost, last 30 days: <span style={{ fontFamily: mono, color: A.ink }}>${cost30d.toFixed(4)}</span>
        </div>
      </div>

      {tab === "jobs" && (
        <>
          {/* Status filter chips — clicking sets the status filter, clicking the active one clears it. */}
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 16 }}>
            {byStatus.map(s => (
              <button key={s.status} onClick={() => update({ status: status === s.status ? "" : s.status })}
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
            pageSizeOptions={PAGE_SIZES}
            total={total}
            supportsServer={supportsServer}
            onPageChange={(pi, ps) => update(ps !== pageSize ? { pageSize: ps, page: 0 } : { page: pi })}
            sort={sort}
            sortDir={dir}
            onSortChange={(s, d) => update({ sort: s, dir: d, page: 0 })}
            kind={kind}
            status={status}
            createdBy={createdBy}
            createdByOptions={createdByOptions}
            since={since}
            until={until}
            q={q}
            onKindChange={(v) => update({ kind: v })}
            onStatusChange={(v) => update({ status: v })}
            onCreatedByChange={(v) => update({ createdBy: v })}
            onSinceChange={(v) => update({ since: v })}
            onUntilChange={(v) => update({ until: v })}
            onQChange={(v) => update({ q: v })}
            onRowClick={(id) => update({ job: id })}
            onAct={act}
            loading={isLoading && !data}
            error={isError ? (error instanceof Error ? error.message : "Could not load jobs") : null}
            onRetry={() => refetch()}
          />
        </>
      )}

      {tab === "queue" && (
        <QueuePanel health={health} kinds={kinds} now={now} />
      )}

      {tab === "kinds" && (
        <JobKinds kinds={kinds} counts={counts} onPick={(k, s) => update({ kind: k, status: s, tab: "jobs" })} />
      )}

      {open && (
        <JobDrawer
          jobId={open}
          refetchMs={REFRESH_MS}
          expectedSeconds={openJob ? expected(openJob.kind) : undefined}
          maxReleases={maxReleases}
          eta={etas[open] ?? null}
          onClose={() => update({ job: null })}
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
