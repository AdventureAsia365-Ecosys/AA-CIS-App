"use client";
// app/admin/s1-rewrite/page.tsx — Admin S1 Rewrite v2
// GET  /api/admin/tours                   → all tours with rewrite_count
// GET  /api/admin/tours/{id}/history      → rewrite history
// GET  /api/admin/tours/{id}/detail       → raw + generated + published
// POST /api/admin/run-tour                → trigger rewrite
// AA-994: every string comes from messages.ts (English default, Vietnamese when picked).

import React, { useState, useEffect, useCallback, useRef } from "react";
import { Play, RefreshCw, ArrowRight, Search, CheckCircle, XCircle, Loader2, ChevronRight } from "lucide-react";
import {
  Button, PageFrame, PageFrameBody, PageFrameHeader, PageHeader, useFormat, useT, type Translator, Checkbox, Input, Select,
  K, sans, mono, Section, Badge, LoadingScreen,
  DataTable, Drawer, LinkButton, Modal, type ColumnDef, ToastProvider, useToast, useIsPhone,
} from "../../_kit";
import { phoneCardColumns } from "@/lib/tableSort";
import { TourDetailPanelV2 } from "../_components/TourDetailPanelV2";
import { CompareModal } from "../_components/CompareModal";
import { Pagination } from "../_components/Pagination";
import AdminLiveWriter from "../_components/AdminLiveWriter";  // AA-667
import { M } from "./messages";

type T = Translator<typeof M.en>;

const TENANT_ID = "00000000-0000-0000-0000-000000000001";
const PAGE_SIZE = 20;

function stripUuidPrefix(filename: string | null | undefined): string {
  if (!filename) return "—";
  return filename.replace(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}_/i, "");
}

function relativeTime(isoStr: string | null | undefined, t: T, formatDate: (d: string) => string): string {
  if (!isoStr) return "—";
  const diff = Math.floor((Date.now() - new Date(isoStr).getTime()) / 1000);
  if (diff < 60) return t("justNow");
  if (diff < 3600) return t("minutesAgo", Math.floor(diff / 60));
  if (diff < 86400) return t("hoursAgo", Math.floor(diff / 3600));
  return formatDate(isoStr);
}

// ── Types ─────────────────────────────────────────────────────────────────────

interface Tour {
  tour_id: string;
  src_name: string;
  country: string | null;
  pipeline_status: string;
  ingest_at: string | null;
  source_id: string | null;
  batch_id: string | null;
  filename: string | null;
  rewrite_count: number;
  last_rewritten_at: string | null;
  in_review?: boolean;
}

interface BrandSummary {
  id: string;
  brand_name: string;
  brand_type: string | null;
  version: number;
  is_active: boolean;
  updated_at: string | null;
}

type TourRunStatus = "idle" | "running" | "done" | "failed";

interface RunResult {
  tour_id: string;
  status: string;
  quality_score: number | null;
  version_id: string | null;
  error?: string;
}


// ── Stage progress bar (AA-250 B2) ──────────────────────────────────────────────
// Maps the 7 real LangGraph node names (services/content_generation/graph.py::build_graph,
// confirmed via STEP 0 code read) down to 4 user-facing steps. validate/llm_judge/
// increment_retry collapse to one step because they're the retry loop the user perceives as
// a single "scoring" phase; brand_audit/flag_fix collapse likewise.
const STAGE_STEPS = [
  { step: 1, label: "stageGenerating" },
  { step: 2, label: "stageValidating" },
  { step: 3, label: "stageBrandAudit" },
  { step: 4, label: "stageFinal" },
] as const;

const STAGE_TO_STEP: Record<string, number> = {
  generate:        1,
  validate:        2,
  llm_judge:       2,
  increment_retry: 2,
  brand_audit:     3,
  flag_fix:        3,
  revalidate:      4,
};

function StageProgressBar({ stage, isRetry }: { stage: string | null | undefined; isRetry: boolean }) {
  const t = useT(M);
  const rawStep = stage ? (STAGE_TO_STEP[stage] ?? 1) : 0;
  // A job that has looped generate -> validate -> llm_judge -> increment_retry -> generate is
  // still conceptually "in the validate-retry loop", not starting over — pin the bar at step 2
  // instead of jumping back to step 1 on re-entering generate (spec: don't regress the step).
  const step = isRetry && rawStep === 1 ? 2 : rawStep;

  return (
    <div style={{ display: "flex", alignItems: "center", gap: 4, marginTop: 3 }}>
      {STAGE_STEPS.map((s, i) => (
        <React.Fragment key={s.step}>
          <div
            title={t(s.label)}
            style={{
              width: 7, height: 7, borderRadius: "50%", flexShrink: 0,
              background: step > s.step ? K.success : step === s.step ? K.amber : K.line,
            }}
          />
          {i < STAGE_STEPS.length - 1 && (
            <div style={{ width: 12, height: 2, background: step > s.step ? K.success : K.line, flexShrink: 0 }} />
          )}
        </React.Fragment>
      ))}
      <span style={{ fontSize: 11, color: K.muted, marginLeft: 5, whiteSpace: "nowrap" as const }}>
        {step > 0 ? t(STAGE_STEPS[step - 1].label) : t("queued")}
      </span>
      {isRetry && step === 2 && <Badge tone="warning">{t("retry")}</Badge>}
    </div>
  );
}

// ── Pipeline status badge ─────────────────────────────────────────────────────

function PipelineStatusBadge({ tour, runStatus, result, stage, isRetry }: {
  tour: Tour;
  runStatus: TourRunStatus;
  result?: RunResult;
  stage?: string | null;
  isRetry?: boolean;
}) {
  const t = useT(M);
  if (runStatus === "running") {
    return (
      <div>
        <span style={{ display: "inline-flex", alignItems: "center", gap: 5, fontSize: 12, color: K.amber }}>
          <Loader2 size={12} style={{ animation: "spin 1s linear infinite" }} /> {t("processingEllipsis")}
        </span>
        <StageProgressBar stage={stage} isRetry={!!isRetry} />
      </div>
    );
  }
  if (runStatus === "done") {
    const score = result?.quality_score;
    return (
      <span style={{ display: "inline-flex", alignItems: "center", gap: 5, fontSize: 12, color: K.success }}>
        <CheckCircle size={12} /> {t("done")}{score != null ? ` · ${score.toFixed(1)}` : ""}
      </span>
    );
  }
  if (runStatus === "failed") {
    return (
      <span style={{ display: "inline-flex", alignItems: "center", gap: 5, fontSize: 12, color: K.danger }} title={result?.error}>
        <XCircle size={12} /> {t("failed")}
      </span>
    );
  }

  const { pipeline_status, rewrite_count } = tour;
  if (pipeline_status === "processing") {
    return (
      <span style={{ display: "inline-flex", alignItems: "center", gap: 5, fontSize: 12, color: K.amber }}>
        <Loader2 size={12} style={{ animation: "spin 1s linear infinite" }} /> {t("processing")}
      </span>
    );
  }
  if (pipeline_status === "published") {
    return <Badge tone="success">{t("published")}</Badge>;
  }
  // S218: every other status used to fall through to "Ready" (hitl_rejected tours with a Master).
  if (pipeline_status === "hitl_rejected") return <Badge tone="danger">{t("rejected")}</Badge>;
  if (pipeline_status === "failed") return <Badge tone="danger">{t("failed")}</Badge>;
  if (tour.in_review) return <Badge tone="warning">{t("inReview", rewrite_count)}</Badge>;
  if (pipeline_status === "ingested" && rewrite_count > 0) {
    return <Badge tone="success">{t("readyRewritten", rewrite_count)}</Badge>;
  }
  if (pipeline_status !== "ingested") return <Badge tone="neutral">{pipeline_status}</Badge>;
  return <Badge tone="neutral">{t("ready")}</Badge>;
}

// ── Main page ─────────────────────────────────────────────────────────────────

export default function S1RewritePage() {
  // AA-1006: the "rewrite complete" note is a kit toast, painted above the live writer Drawer.
  return (
    <ToastProvider>
      <S1RewritePageInner />
    </ToastProvider>
  );
}

function S1RewritePageInner() {
  const t = useT(M);
  const tr = t; // the tour table's rows are `t` (the tour), so the table reads the translator as `tr`
  const { formatDate } = useFormat();
  // The run loop and the polling interval below build messages long after a render: they read the current
  // language's translator through this ref, like the other refs of this page.
  const tRef = useRef(t);
  useEffect(() => { tRef.current = t; }, [t]);
  const toast = useToast();
  const isPhone = useIsPhone();
  const [tours, setTours]                   = useState<Tour[]>([]);
  const [loading, setLoading]               = useState(true);
  const [selectedIds, setSelectedIds]       = useState<Set<string>>(new Set());
  const [filterCountry, setFilterCountry]   = useState("");
  const [filterFile, setFilterFile]         = useState("");
  const [filterStatus, setFilterStatus]     = useState("");
  const [filterSearch, setFilterSearch]     = useState("");
  const [page, setPage]                     = useState(1);
  const [seoMode, setSeoMode]               = useState("standard");
  // AA-702 / S262 C4 (root ADR 0009 UI "the per-run model_tier select leaves /admin/s1-rewrite"): a run never picks
  // its own model. It sends no model_tier, so the s1_generate route in AI providers › Routing applies (an explicit
  // model_tier would win over the route in LLMClient); the page only shows which model that is.
  const [seoPrefetch, setSeoPrefetch]       = useState<{ state: "running" | "done" | "failed"; text: string } | null>(null);
  const [s1Model, setS1Model]               = useState("");
  const [brandList, setBrandList]           = useState<BrandSummary[]>([]);
  const [brandName, setBrandName]           = useState<string | null>(null);
  const [brandId, setBrandId]               = useState<string | null>(null);
  const [showConfirm, setShowConfirm]       = useState(false);
  const [running, setRunning]               = useState(false);
  const [tourStatuses, setTourStatuses]     = useState<Record<string, TourRunStatus>>({});
  const [runResults, setRunResults]         = useState<Record<string, RunResult>>({});
  const [runComplete, setRunComplete]       = useState(false);
  const [detailTour, setDetailTour]         = useState<Tour | null>(null);
  const [compareOpen, setCompareOpen]       = useState(false);
  const [jobIds, setJobIds]                 = useState<Record<string, string>>({}); // tour_id -> job_id
  // AA-667: tour whose live writing panel is open ({tourId, jobId, name}).
  const [liveTour, setLiveTour]             = useState<{ tourId: string; jobId: string; name: string } | null>(null);
  // AA-250 B2: tour_id -> last-seen current_stage from GET /admin/jobs/{id}, and whether that
  // job has looped back into "generate" more than once (validate-retry loop indicator).
  const [tourStages, setTourStages]         = useState<Record<string, string | null>>({});
  const [tourRetryFlags, setTourRetryFlags] = useState<Record<string, boolean>>({});

  // AA-248: async run-tour + polling. Refs mirror state for use inside the
  // polling interval / worker loop, which close over values once and would
  // otherwise see stale state (same pattern as CatalogTab.tsx).
  const queueRef          = useRef<Tour[]>([]);
  const activeWorkersRef  = useRef(0);
  const runTourIdsRef     = useRef<string[]>([]);
  const waitersRef        = useRef<Record<string, () => void>>({});
  const pollingRef        = useRef<ReturnType<typeof setInterval> | null>(null);
  const jobIdsRef         = useRef<Record<string, string>>({});
  const tourStatusesRef   = useRef<Record<string, TourRunStatus>>({});
  const toursRef          = useRef<Tour[]>([]);
  // AA-250 B2: last distinct stage seen + how many times "generate" was entered, per tour_id —
  // used to detect the validate-retry loop (generate entered a 2nd+ time) without needing the
  // backend to expose retry_count.
  const tourStagesRef        = useRef<Record<string, string | null>>({});
  const generateEntryCountRef = useRef<Record<string, number>>({});

  useEffect(() => { jobIdsRef.current = jobIds; }, [jobIds]);
  useEffect(() => { tourStatusesRef.current = tourStatuses; }, [tourStatuses]);
  useEffect(() => { toursRef.current = tours; }, [tours]);

  function resolveWaiter(tourId: string) {
    const resolve = waitersRef.current[tourId];
    if (resolve) {
      delete waitersRef.current[tourId];
      resolve();
    }
  }

  useEffect(() => {
    fetch("/api/admin/llm-config")
      .then(r => (r.ok ? r.json() : null))
      .then(d => {
        type StageRow = { stage: string; model_id: string; options?: { model_id: string; label?: string }[] };
        const row = ((d?.stages ?? []) as StageRow[]).find((x) => x.stage === "s1_generate");
        if (!row) return;
        const cur = (row.options ?? []).find((o) => o.model_id === row.model_id);
        setS1Model(cur?.label ?? row.model_id);
      })
      .catch(() => {});
  }, []);

  // The fetch alone (no state set before the first await), so the mount effect can call it; `loading` starts true.
  const fetchTours = useCallback(() =>
    fetch("/api/admin/tours")
      .then(async (res) => {
        if (!res.ok) throw new Error(await res.text());
        return res.json();
      })
      .then((data) => setTours(data.tours || []))
      .finally(() => setLoading(false)), []);
  const loadTours = useCallback(async () => {
    setLoading(true);
    await fetchTours();
  }, [fetchTours]);

  const handleRefresh = useCallback(async () => {
    if (running) return;
    setTourStatuses({});
    setRunResults({});
    setRunComplete(false);
    await loadTours();
  }, [loadTours, running]);

  const loadBrandList = useCallback(() =>
    fetch(`/api/admin/brand-rules?tenant_id=${TENANT_ID}`)
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (data == null) return;
        const list: BrandSummary[] = Array.isArray(data) ? data : [];
        setBrandList(list);
        const active = list.find(b => b.is_active) ?? list[0];
        if (active) { setBrandId(active.id); setBrandName(active.brand_name); }
      })
      .catch(() => {}), []);

  useEffect(() => { fetchTours(); loadBrandList(); }, [fetchTours, loadBrandList]);

  const uniqueCountries = Array.from(new Set(
    tours.map(t => t.country).filter((c): c is string => Boolean(c))
  )).sort();
  const uniqueFiles = Array.from(new Set(
    tours.map(t => t.filename).filter((f): f is string => Boolean(f))
  )).sort();

  const filteredTours = tours.filter(t => {
    if (filterCountry && t.country !== filterCountry) return false;
    if (filterFile && t.filename !== filterFile) return false;
    if (filterSearch && !t.src_name.toLowerCase().includes(filterSearch.toLowerCase())) return false;
    if (filterStatus === "published" && t.pipeline_status !== "published") return false;
    if (filterStatus === "ready"     && (t.pipeline_status !== "ingested" || t.in_review)) return false;
    if (filterStatus === "review"    && !t.in_review) return false;
    return true;
  });

  const paginatedTours = filteredTours.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

  function handleFilterChange(setter: (v: string) => void, v: string) {
    setter(v);
    setPage(1);
  }

  function selectAll()   { setSelectedIds(new Set(filteredTours.map(t => t.tour_id))); }
  function deselectAll() { setSelectedIds(new Set()); }
  const toggleSelected = (tourId: string) =>
    setSelectedIds(prev => { const next = new Set(prev); if (next.has(tourId)) next.delete(tourId); else next.add(tourId); return next; });

  // AA-1006: the tour list is a kit DataTable (the page keeps its own selection, filters and pager).
  const tourColumns: ColumnDef<Tour, unknown>[] = [
    {
      id: "select", enableSorting: false, enableHiding: false, meta: { hideLabelOnCard: true },
      header: () => (
        <Checkbox
          aria-label={tr("selectAll", filteredTours.length)}
          checked={filteredTours.length > 0 && filteredTours.every(x => selectedIds.has(x.tour_id))}
          onChange={e => e.target.checked ? selectAll() : deselectAll()}
        />
      ),
      cell: ({ row }) => (
        <span onClick={e => e.stopPropagation()}>
          <Checkbox
            aria-label={row.original.src_name}
            checked={selectedIds.has(row.original.tour_id)}
            onChange={() => toggleSelected(row.original.tour_id)}
            disabled={running}
          />
        </span>
      ),
    },
    { id: "name", header: tr("colTourName"), accessorKey: "src_name",
      cell: ({ row }) => <span style={{ fontWeight: 500, color: K.ink }}>{row.original.src_name}</span> },
    { id: "country", header: tr("colCountry"), accessorFn: (x) => x.country ?? "",
      cell: ({ row }) => <span style={{ color: row.original.country ? K.body : K.muted2 }}>{row.original.country || "—"}</span> },
    { id: "file", header: tr("colSourceFile"), accessorFn: (x) => x.filename ?? "",
      cell: ({ row }) => <span style={{ fontFamily: mono, fontSize: 11, color: K.muted }}>{stripUuidPrefix(row.original.filename)}</span> },
    { id: "rewrites", header: tr("colRewrites"), accessorKey: "rewrite_count", meta: { align: "right" },
      cell: ({ row }) => row.original.rewrite_count > 0
        ? <span style={{ fontFamily: mono, fontSize: 13, fontWeight: 600, color: K.ink }}>{row.original.rewrite_count}</span>
        : <span style={{ color: K.muted2 }}>—</span> },
    { id: "lastRewritten", header: tr("colLastRewritten"), accessorFn: (x) => x.last_rewritten_at ?? "",
      cell: ({ row }) => <span style={{ color: K.muted2, fontSize: 12 }}>{relativeTime(row.original.last_rewritten_at, tr, formatDate)}</span> },
    { id: "ingested", header: tr("colIngested"), accessorFn: (x) => x.ingest_at ?? "",
      cell: ({ row }) => <span style={{ color: K.muted2, fontSize: 12 }}>{relativeTime(row.original.ingest_at, tr, formatDate)}</span> },
    { id: "status", header: tr("colStatus"), enableSorting: false, cell: ({ row: { original: x } }) => {
      const runStatus = tourStatuses[x.tour_id] || "idle";
      return (
        <div>
          <PipelineStatusBadge
            tour={x} runStatus={runStatus} result={runResults[x.tour_id]}
            stage={tourStages[x.tour_id]} isRetry={!!tourRetryFlags[x.tour_id]}
          />
          {/* AA-667: open the live writing panel while this tour is running */}
          {runStatus === "running" && jobIds[x.tour_id] && (
            <LinkButton size="sm" style={{ marginTop: 4, fontSize: 11 }}
              onClick={(e) => {
                e.stopPropagation();
                setLiveTour({ tourId: x.tour_id, jobId: jobIds[x.tour_id], name: x.src_name });
              }}>
              {tr("liveView")}
            </LinkButton>
          )}
        </div>
      );
    } },
    { id: "open", header: "", enableSorting: false, enableHiding: false, meta: { hideLabelOnCard: true },
      cell: () => <ChevronRight size={14} color={K.muted2} /> },
  ];

  const selectedTours = tours.filter(t => selectedIds.has(t.tour_id));

  // AA-248: fires the async job and returns immediately once queued/running —
  // it does NOT await completion. The polling effect below tracks the job
  // through to succeeded/failed and updates tourStatuses accordingly.
  async function runSingleTour(tour: Tour): Promise<void> {
    setTourStatuses(prev => ({ ...prev, [tour.tour_id]: "running" }));
    try {
      const res = await fetch("/api/admin/run-tour-async", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          tour_id:              tour.tour_id,
          batch_id:             tour.batch_id || TENANT_ID,
          tenant_id:            TENANT_ID,
          seo_mode:             seoMode,
          brand_identity_id:    brandId || undefined,
          brand_name:           brandName || undefined,
        }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }));
        throw new Error(err.detail || tRef.current("runFailed"));
      }
      const data: { job_id: string; status: string } = await res.json();
      setJobIds(prev => ({ ...prev, [tour.tour_id]: data.job_id }));
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : tRef.current("unknownError");
      setRunResults(prev => ({
        ...prev,
        [tour.tour_id]: { tour_id: tour.tour_id, status: "failed", quality_score: null, version_id: null, error: msg },
      }));
      setTourStatuses(prev => ({ ...prev, [tour.tour_id]: "failed" }));
      resolveWaiter(tour.tour_id);
    }
  }

  // 3-worker pool: each worker pulls the next tour off queueRef, fires the
  // async job, then waits for that tour's status to leave "running" (resolved
  // either immediately on dispatch failure, or by the polling effect once the
  // job settles) before pulling the next one. Caps concurrent in-flight jobs
  // at 3 regardless of how many tours were selected.
  async function runWorker() {
    activeWorkersRef.current += 1;
    try {
      while (queueRef.current.length > 0) {
        const tour = queueRef.current.shift();
        if (!tour) break;
        const waitPromise = new Promise<void>(resolve => { waitersRef.current[tour.tour_id] = resolve; });
        await runSingleTour(tour);
        await waitPromise;
      }
    } finally {
      activeWorkersRef.current -= 1;
    }
  }

  async function prefetchSeo(tourIds: string[]): Promise<void> {
    setSeoPrefetch({ state: "running", text: tRef.current("seoFetching", tourIds.length) });
    try {
      const res = await fetch("/api/admin/s1/seo-prefetch", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tour_ids: tourIds, tenant_id: TENANT_ID }),
      });
      if (!res.ok) throw new Error(await res.text());
      const { job_id } = await res.json();
      // AA-747: return once the job exists (so a rewrite sees it and waits for its tour's SEO row
      // instead of buying); progress is followed in the background.
      void watchPrefetch(job_id);
    } catch (e) {
      setSeoPrefetch({ state: "failed", text: tRef.current("seoCouldNotStart", String(e).slice(0, 80)) });
    }
  }

  async function watchPrefetch(job_id: string): Promise<void> {
    try {
      const deadline = Date.now() + 15 * 60 * 1000;
      while (Date.now() < deadline) {
        await new Promise(r => setTimeout(r, 4000));
        const j = await (await fetch(`/api/admin/job-runner/jobs/${job_id}`)).json();
        const st = j.status ?? j.job?.status;
        if (["succeeded", "failed", "stopped_budget", "cancelled"].includes(st)) {
          const r = j.result ?? j.job?.result ?? {};
          setSeoPrefetch({ state: st === "succeeded" ? "done" : "failed",
            text: st === "succeeded"
              ? tRef.current("seoReady", r.reused ?? 0, r.from_research_cache ?? 0, r.ideas_tasks ?? 0, r.serp_calls ?? 0)
              : tRef.current("seoEnded", st) });
          return;
        }
      }
      setSeoPrefetch({ state: "failed", text: tRef.current("seoStillRunning") });
    } catch (e) {
      setSeoPrefetch({ state: "failed", text: tRef.current("seoUnavailable", String(e).slice(0, 80)) });
    }
  }

  async function startRun() {
    setShowConfirm(false);
    setRunning(true);
    setRunComplete(false);
    setTourStatuses({});
    setRunResults({});
    setJobIds({});
    setTourStages({});
    setTourRetryFlags({});
    tourStagesRef.current = {};
    generateEntryCountRef.current = {};
    waitersRef.current = {};

    queueRef.current = [...selectedTours];
    runTourIdsRef.current = selectedTours.map(t => t.tour_id);

    // AA-653 — buy the selected tours' DataForSEO data in shared tasks (reused for a year), so
    // each rewrite below finds it and buys nothing. A failed prefetch only means the rewrites
    // fetch per tour as before.
    // AA-747 — the prefetch is NOT awaited: the rewrite workers start at the SAME time. The
    // prefetch job commits each batch of 5 tours' seo_context as it goes (s1_prefetch.py), and the
    // s1_rewrite job waits for its own tour's row (cap 5 min) before fetching per tour, so early
    // tours are ready within ~1 min instead of blocking the whole wave on the full ~8 min prefetch.
    // AA-747: await only the job creation (not its completion) — rewrites start right away.
    if (seoMode !== "minimal") await prefetchSeo(selectedTours.map(t => t.tour_id));

    const workerCount = Math.min(3, queueRef.current.length);
    await Promise.all(Array.from({ length: workerCount }, () => runWorker()));
  }

  // Finalize once every dispatched tour has left "running" (done or failed) —
  // mirrors the old end-of-run behavior (reload tours, clear transient state,
  // show the completion CTA), but triggered by polling instead of an await chain.
  useEffect(() => {
    if (!running) return;
    const ids = runTourIdsRef.current;
    if (ids.length === 0) return;
    const allSettled = ids.every(id => tourStatuses[id] === "done" || tourStatuses[id] === "failed");
    if (!allSettled) return;

    setRunning(false);
    setTourStatuses({});
    setRunResults({});
    setJobIds({});
    setTourStages({});
    setTourRetryFlags({});
    tourStagesRef.current = {};
    generateEntryCountRef.current = {};
    setRunComplete(true);
    loadTours();
  }, [running, tourStatuses, loadTours]);

  // Poll GET /admin/jobs/{job_id} every 5s for any tour still "running",
  // modeled on CatalogTab.tsx's polling pattern (pollingRef guard, 5min cutoff).
  useEffect(() => {
    const anyRunning = Object.values(tourStatuses).some(s => s === "running");

    if (!anyRunning) {
      if (pollingRef.current) { clearInterval(pollingRef.current); pollingRef.current = null; }
      return;
    }
    if (pollingRef.current) return; // already polling

    const startTime = Date.now();
    pollingRef.current = setInterval(async () => {
      if (Date.now() - startTime > 300_000) {
        const timedOutIds = Object.keys(jobIdsRef.current).filter(id => tourStatusesRef.current[id] === "running");
        timedOutIds.forEach(tourId => {
          setRunResults(prev => ({
            ...prev,
            [tourId]: {
              tour_id: tourId, status: "failed", quality_score: null, version_id: null,
              error: tRef.current("noResponse"),
            },
          }));
          setTourStatuses(prev => ({ ...prev, [tourId]: "failed" }));
          resolveWaiter(tourId);
        });
        if (pollingRef.current) { clearInterval(pollingRef.current); pollingRef.current = null; }
        return;
      }

      const runningIds = Object.keys(jobIdsRef.current).filter(id => tourStatusesRef.current[id] === "running");
      await Promise.all(runningIds.map(async (tourId) => {
        const jobId = jobIdsRef.current[tourId];
        try {
          const res = await fetch(`/api/admin/jobs/${jobId}`);
          if (!res.ok) return;
          const job = await res.json();

          // AA-250 B2: track current_stage + count distinct entries into "generate" so the
          // stepper can detect a validate-retry loop (backend doesn't expose retry_count).
          const newStage: string | null = job.current_stage ?? null;
          if (newStage && newStage !== tourStagesRef.current[tourId]) {
            if (newStage === "generate" && tourStagesRef.current[tourId] !== "generate") {
              generateEntryCountRef.current[tourId] = (generateEntryCountRef.current[tourId] || 0) + 1;
              if (generateEntryCountRef.current[tourId] > 1) {
                setTourRetryFlags(prev => ({ ...prev, [tourId]: true }));
              }
            }
            tourStagesRef.current[tourId] = newStage;
            setTourStages(prev => ({ ...prev, [tourId]: newStage }));
          }

          if (job.status === "succeeded") {
            let qualityScore: number | null = null;
            try {
              const histRes = await fetch(`/api/admin/tours/${tourId}/history`);
              if (histRes.ok) {
                const histData = await histRes.json();
                const entry = (histData.history || []).find((h: { id: string }) => h.id === job.result_version_id);
                qualityScore = entry?.score_overall ?? null;
              }
            } catch {}
            setRunResults(prev => ({
              ...prev,
              [tourId]: { tour_id: tourId, status: "success", quality_score: qualityScore, version_id: job.result_version_id },
            }));
            setTourStatuses(prev => ({ ...prev, [tourId]: "done" }));
            const tourName = toursRef.current.find(x => x.tour_id === tourId)?.src_name ?? tRef.current("tour");
            toast.success(tRef.current("rewriteComplete", tourName), 5000);
            resolveWaiter(tourId);
          } else if (job.status === "failed" || job.status === "interrupted") {
            setRunResults(prev => ({
              ...prev,
              [tourId]: { tour_id: tourId, status: "failed", quality_score: null, version_id: null, error: job.error ?? tRef.current("jobFailed") },
            }));
            setTourStatuses(prev => ({ ...prev, [tourId]: "failed" }));
            resolveWaiter(tourId);
          }
          // "queued" / "running": leave as-is, no change.
        } catch {}
      }));
    }, 5000);
  }, [tourStatuses, toast]);

  // Cleanup polling on unmount
  useEffect(() => () => {
    if (pollingRef.current) { clearInterval(pollingRef.current); pollingRef.current = null; }
  }, []);

  const statusCounts = Object.values(tourStatuses).reduce(
    (acc, s) => { acc[s] = (acc[s] || 0) + 1; return acc; },
    {} as Record<TourRunStatus, number>
  );

  const seoModeLabel = (m: string) => ({
    standard:   t("seoStandard"),
    aggressive: t("seoAggressive"),
    minimal:    t("seoMinimal"),
  }[m] ?? m);

  const modelLabel = () => t("settingsDefaultModel", s1Model);

  if (loading) {
    return (
        <PageFrame>
          <LoadingScreen message={t("loadingTours")} />
        </PageFrame>
    );
  }

  return (
    <>
      <PageFrame variant="split" style={{ overflow: "hidden" }}>

        {/* ── Sticky Config Panel ─────────────────────────────────────────── */}
        <PageFrameHeader style={{
          position: "sticky", top: 0, zIndex: 20, background: K.card,
          padding: "12px 36px",
        }}>
          {/* S252 — flex-wrap, not a fixed 4-column grid: at phone width the three selects and the buttons stack. */}
          <div style={{ display: "flex", flexWrap: "wrap", gap: 12, alignItems: "flex-end" }}>
            <div style={{ flex: "1 1 200px", minWidth: 0 }}>
              <label style={{ fontSize: 11, color: K.muted, display: "block", marginBottom: 4 }}>{t("brandIdentity")}</label>
              <Select
                value={brandId ?? ""}
                onChange={e => {
                  const id = e.target.value || null;
                  setBrandId(id);
                  const picked = brandList.find(b => b.id === id);
                  setBrandName(picked ? picked.brand_name : null);
                }}
                disabled={running}
                style={{ width: "100%", padding: "7px 10px", borderRadius: 6, border: `1px solid ${K.line}`, fontSize: 13, fontFamily: sans, background: K.card }}
              >
                {brandList.length === 0 && <option value="">{t("noBrand")}</option>}
                {brandList.map(b => (
                  <option key={b.id} value={b.id}>
                    {b.brand_name} · v{b.version}{b.is_active ? t("active") : ""}
                  </option>
                ))}
              </Select>
            </div>
            <div style={{ flex: "1 1 200px", minWidth: 0 }}>
              <label style={{ fontSize: 11, color: K.muted, display: "block", marginBottom: 4 }}>{t("seoMode")}</label>
              <Select
                value={seoMode}
                onChange={e => setSeoMode(e.target.value)}
                disabled={running}
                style={{ width: "100%", padding: "7px 10px", borderRadius: 6, border: `1px solid ${K.line}`, fontSize: 13, fontFamily: sans, background: K.card }}
              >
                <option value="standard">{t("seoStandardOption")}</option>
                <option value="aggressive">{t("seoAggressiveOption")}</option>
                <option value="minimal">{t("seoMinimalOption")}</option>
              </Select>
            </div>
            <div style={{ flex: "1 1 200px", minWidth: 0 }}>
              <label style={{ fontSize: 11, color: K.muted, display: "block", marginBottom: 4 }}>{t("model")}</label>
              <div data-testid="s1-route-model"
                style={{ padding: "7px 0", fontSize: 13, color: K.ink, fontFamily: sans, overflowWrap: "anywhere" }}>
                {s1Model || "—"}{" "}
                <a href="/admin/ai-gateway?tab=routing" style={{ fontSize: 12, color: K.accent }}>{t("routeLink")}</a>
              </div>
            </div>
            <div style={{ display: "flex", gap: 8, alignItems: "flex-end", flexWrap: "wrap" }}>
              {selectedIds.size >= 2 && selectedIds.size <= 4 && (
                <Button
                  variant="secondary"
                  size="lg"
                  onClick={() => setCompareOpen(true)}
                  disabled={running}
                  style={{ whiteSpace: "nowrap" as const }}
                >
                  {t("compare", selectedIds.size)}
                </Button>
              )}
              <Button
                variant="primary"
                size="lg"
                disabled={running || selectedIds.size === 0}
                onClick={() => { setTourStatuses({}); setRunResults({}); setRunComplete(false); setShowConfirm(true); }}
                style={{
                  background: (running || selectedIds.size === 0) ? K.muted : K.accent,
                  border: `1px solid ${(running || selectedIds.size === 0) ? K.muted : K.accent}`,
                  display: "flex", alignItems: "center", gap: 8,
                  whiteSpace: "nowrap" as const,
                  opacity: selectedIds.size === 0 ? 0.5 : 1,
                }}
              >
                <Play size={14} />
                {running ? t("running") : t("runRewrite", selectedIds.size)}
              </Button>
            </div>
          </div>
        </PageFrameHeader>

        {/* ── Scrollable Content ──────────────────────────────────────────── */}
        <PageFrameBody scroll style={{ paddingTop: 24 }}>

          {/* Header */}
          <PageHeader
            title={t("title")}
            description={
              <>
                <span style={{ display: "block" }}>{t("toursTotal", tours.length)}</span>
                {seoPrefetch && (
                  <span style={{ display: "block", fontSize: 12, marginTop: 6,
                    color: seoPrefetch.state === "failed" ? K.danger : seoPrefetch.state === "done" ? K.success : K.accent }}>
                    {seoPrefetch.text}
                  </span>
                )}
              </>
            }
            actions={
              <Button size="sm" variant="ghost" onClick={handleRefresh} style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <RefreshCw size={13} /> {t("refresh")}
              </Button>
            }
          />

          {/* Filter bar */}
          <Section style={{ marginBottom: 16 }} bodyStyle={{ padding: "12px 16px" }}>
            <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
              <Select
                value={filterCountry}
                onChange={e => handleFilterChange(setFilterCountry, e.target.value)}
                style={{ padding: "6px 10px", borderRadius: 6, border: `1px solid ${K.line}`, fontSize: 13, fontFamily: sans, background: K.card, minWidth: 140 }}
              >
                <option value="">{t("allCountries")}</option>
                {uniqueCountries.map(c => <option key={c} value={c}>{c}</option>)}
              </Select>

              <Select
                value={filterFile}
                onChange={e => handleFilterChange(setFilterFile, e.target.value)}
                style={{ padding: "6px 10px", borderRadius: 6, border: `1px solid ${K.line}`, fontSize: 13, fontFamily: sans, background: K.card, minWidth: 180 }}
              >
                <option value="">{t("allFiles")}</option>
                {uniqueFiles.map(f => <option key={f} value={f}>{stripUuidPrefix(f)}</option>)}
              </Select>

              <Select
                value={filterStatus}
                onChange={e => handleFilterChange(setFilterStatus, e.target.value)}
                style={{ padding: "6px 10px", borderRadius: 6, border: `1px solid ${K.line}`, fontSize: 13, fontFamily: sans, background: K.card, minWidth: 130 }}
              >
                <option value="">{t("allStatus")}</option>
                <option value="published">{t("filterPublished")}</option>
                <option value="ready">{t("filterReady")}</option>
                <option value="review">{t("filterReview")}</option>
              </Select>

              <div style={{ display: "flex", alignItems: "center", gap: 6, border: `1px solid ${K.line}`, borderRadius: 6, padding: "6px 10px", background: K.card, flex: 1, minWidth: 200 }}>
                <Search size={13} style={{ color: K.muted2, flexShrink: 0 }} />
                <Input
                  placeholder={t("searchTour")}
                  value={filterSearch}
                  onChange={e => handleFilterChange(setFilterSearch, e.target.value)}
                  style={{ border: "none", outline: "none", fontSize: 13, fontFamily: sans, width: "100%", background: "transparent" }}
                />
              </div>

              <Button size="sm" variant="ghost" onClick={selectAll}>{t("selectAll", filteredTours.length)}</Button>
              <Button size="sm" variant="ghost" onClick={deselectAll} disabled={selectedIds.size === 0}>{t("deselectAll")}</Button>
            </div>
          </Section>

          {/* Tours table */}
          {tours.length === 0 ? (
            <Section bodyStyle={{ textAlign: "center", padding: "48px 24px" }}>
              <div style={{ fontSize: 15, color: K.muted, marginBottom: 14 }}>{t("noTours")}</div>
              <div style={{ fontSize: 13, color: K.muted2, marginBottom: 20 }}>{t("uploadFirst")}</div>
              <Button size="sm" variant="secondary" onClick={() => window.location.href = "/admin/upload"}
                style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                {t("goToUpload")} <ArrowRight size={12} />
              </Button>
            </Section>
          ) : (
            <Section padded={false} style={{ marginBottom: 20, overflow: "hidden" }}>
              <DataTable<Tour>
                tableId="s1-rewrite-tours"
                data={paginatedTours}
                getRowId={(row) => row.tour_id}
                onRowClick={(row) => setDetailTour(detailTour?.tour_id === row.tour_id ? null : row)}
                searchable={false}
                paginate={false}
                columns={phoneCardColumns(tourColumns, "name", isPhone)}
              />
              {filteredTours.length > PAGE_SIZE && (
                <div style={{ padding: "12px 20px", borderTop: `1px solid ${K.line}`, display: "flex", justifyContent: "flex-end" }}>
                  <Pagination page={page} total={filteredTours.length} pageSize={PAGE_SIZE} onPage={setPage} />
                </div>
              )}
            </Section>
          )}

          {/* Confirm dialog */}
          <Modal
            open={showConfirm}
            onClose={() => setShowConfirm(false)}
            title={t("confirmTitle")}
            width={420}
            footer={
              <>
                <Button size="sm" variant="ghost" onClick={() => setShowConfirm(false)}>{t("cancel")}</Button>
                <Button size="sm" variant="primary" onClick={startRun}>{t("yesRun")}</Button>
              </>
            }
          >
            <div style={{ fontSize: 14, color: K.body, lineHeight: 1.6 }}>
              {t("confirmLead")}{" "}
              <strong>{t("toursCount", selectedIds.size)}</strong>{" "}
              {t("using")} <strong>{t("seoModeName", seoModeLabel(seoMode))}</strong> {t("modeWith")}{" "}
              <strong>{modelLabel()}</strong>
              {brandName ? t("brandSuffix", brandName) : ""}{t("continueQ")}
            </div>
          </Modal>

          {/* AA-667: live writing panel (steps + streamed tour fields) for one running tour */}
          <Drawer open={liveTour != null} onClose={() => setLiveTour(null)} title={liveTour?.name}>
            {liveTour && (
              <AdminLiveWriter
                jobId={liveTour.jobId}
                active={tourStatuses[liveTour.tourId] === "running"}
                title={t("title")}
              />
            )}
          </Drawer>

          {/* Progress summary */}
          {(running || runComplete) && (Object.keys(tourStatuses).length > 0 || runComplete) && (
            <Section style={{ marginBottom: 20 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 16, flexWrap: "wrap" }}>
                <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.14em", color: K.muted }}>
                  {t("progress")}
                </div>
                {Object.keys(tourStatuses).length > 0 && (
                  <span style={{ fontSize: 13, color: K.muted }}>
                    {t("complete", (statusCounts.done || 0) + (statusCounts.failed || 0), Object.keys(tourStatuses).length)}
                  </span>
                )}
                {(statusCounts.done || 0) > 0 && (
                  <span style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 13, color: K.success }}>
                    <CheckCircle size={13} /> {t("doneCount", statusCounts.done)}
                  </span>
                )}
                {(statusCounts.failed || 0) > 0 && (
                  <span style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 13, color: K.danger }}>
                    <XCircle size={13} /> {t("failedCount", statusCounts.failed)}
                  </span>
                )}
                {running && <span style={{ fontSize: 13, color: K.amber }}>{t("processingEllipsis")}</span>}
                {runComplete && (
                  <Button
                    size="sm"
                    variant="primary"
                    onClick={() => window.location.href = "/admin/master-content"}
                    style={{
                      background: K.accent, border: `1px solid ${K.accent}`,
                      display: "flex", alignItems: "center", gap: 6, marginLeft: "auto",
                    }}
                  >
                    {t("viewMaster")} <ArrowRight size={12} />
                  </Button>
                )}
              </div>
            </Section>
          )}
        </PageFrameBody>
      </PageFrame>

      {/* Detail panel v2 (handles its own backdrop) */}
      {detailTour && (
        <TourDetailPanelV2
          tourId={detailTour.tour_id}
          tourName={detailTour.src_name}
          rewriteCount={detailTour.rewrite_count}
          onClose={() => setDetailTour(null)}
        />
      )}

      {/* Compare modal */}
      {compareOpen && (
        <CompareModal
          tourIds={[...selectedIds]}
          onClose={() => setCompareOpen(false)}
        />
      )}

    </>
  );
}
