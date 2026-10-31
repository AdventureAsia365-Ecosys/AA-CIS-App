"use client";
// app/admin/s1-rewrite/page.tsx — Admin S1 Rewrite v2
// GET  /api/admin/tours                   → all tours with rewrite_count
// GET  /api/admin/tours/{id}/history      → rewrite history
// GET  /api/admin/tours/{id}/detail       → raw + generated + published
// POST /api/admin/run-tour                → trigger rewrite
// AA-994: every string comes from messages.ts (English default, Vietnamese when picked).

import React, { useState, useEffect, useCallback, useRef } from "react";
import { Play, RefreshCw, ArrowRight, Search, CheckCircle, XCircle, Loader2, ChevronRight } from "lucide-react";
import { A, alpha, serif, sans, mono, Card, SLabel, Badge, LoadingScreen, TH, TD } from "../_components/adminUi";
import { Button, PageFrame, PageFrameBody, PageFrameHeader, PageHeader, useFormat, useT, type Translator } from "../../_kit";
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
              background: step > s.step ? A.green : step === s.step ? A.amber : A.line,
            }}
          />
          {i < STAGE_STEPS.length - 1 && (
            <div style={{ width: 12, height: 2, background: step > s.step ? A.green : A.line, flexShrink: 0 }} />
          )}
        </React.Fragment>
      ))}
      <span style={{ fontSize: 11, color: A.muted, marginLeft: 5, whiteSpace: "nowrap" as const }}>
        {step > 0 ? t(STAGE_STEPS[step - 1].label) : t("queued")}
      </span>
      {isRetry && step === 2 && <Badge color="amber">{t("retry")}</Badge>}
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
        <span style={{ display: "inline-flex", alignItems: "center", gap: 5, fontSize: 12, color: A.amber }}>
          <Loader2 size={12} style={{ animation: "spin 1s linear infinite" }} /> {t("processingEllipsis")}
        </span>
        <StageProgressBar stage={stage} isRetry={!!isRetry} />
      </div>
    );
  }
  if (runStatus === "done") {
    const score = result?.quality_score;
    return (
      <span style={{ display: "inline-flex", alignItems: "center", gap: 5, fontSize: 12, color: A.green }}>
        <CheckCircle size={12} /> {t("done")}{score != null ? ` · ${score.toFixed(1)}` : ""}
      </span>
    );
  }
  if (runStatus === "failed") {
    return (
      <span style={{ display: "inline-flex", alignItems: "center", gap: 5, fontSize: 12, color: A.red }} title={result?.error}>
        <XCircle size={12} /> {t("failed")}
      </span>
    );
  }

  const { pipeline_status, rewrite_count } = tour;
  if (pipeline_status === "processing") {
    return (
      <span style={{ display: "inline-flex", alignItems: "center", gap: 5, fontSize: 12, color: A.amber }}>
        <Loader2 size={12} style={{ animation: "spin 1s linear infinite" }} /> {t("processing")}
      </span>
    );
  }
  if (pipeline_status === "published") {
    return <Badge color="green">{t("published")}</Badge>;
  }
  // S218: every other status used to fall through to "Ready" (hitl_rejected tours with a Master).
  if (pipeline_status === "hitl_rejected") return <Badge color="red">{t("rejected")}</Badge>;
  if (pipeline_status === "failed") return <Badge color="red">{t("failed")}</Badge>;
  if (tour.in_review) return <Badge color="amber">{t("inReview", rewrite_count)}</Badge>;
  if (pipeline_status === "ingested" && rewrite_count > 0) {
    return <Badge color="green">{t("readyRewritten", rewrite_count)}</Badge>;
  }
  if (pipeline_status !== "ingested") return <Badge color="gray">{pipeline_status}</Badge>;
  return <Badge color="gray">{t("ready")}</Badge>;
}

// ── Main page ─────────────────────────────────────────────────────────────────

export default function S1RewritePage() {
  const t = useT(M);
  const tr = t; // the tour table's rows are `t` (the tour), so the table reads the translator as `tr`
  const { formatDate } = useFormat();
  // The run loop and the polling interval below build messages long after a render: they read the current
  // language's translator through this ref, like the other refs of this page.
  const tRef = useRef(t);
  useEffect(() => { tRef.current = t; }, [t]);
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
  const [toast, setToast]                   = useState<string | null>(null);
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
        const row = (d?.stages ?? []).find((x: any) => x.stage === "s1_generate");
        if (!row) return;
        const cur = (row.options ?? []).find((o: any) => o.model_id === row.model_id);
        setS1Model(cur?.label ?? row.model_id);
      })
      .catch(() => {});
  }, []);

  const loadTours = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch("/api/admin/tours");
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      setTours(data.tours || []);
    } finally {
      setLoading(false);
    }
  }, []);

  const handleRefresh = useCallback(async () => {
    if (running) return;
    setTourStatuses({});
    setRunResults({});
    setRunComplete(false);
    await loadTours();
  }, [loadTours, running]);

  const loadBrandList = useCallback(async () => {
    try {
      const res = await fetch(`/api/admin/brand-rules?tenant_id=${TENANT_ID}`);
      if (!res.ok) return;
      const data = await res.json();
      const list: BrandSummary[] = Array.isArray(data) ? data : [];
      setBrandList(list);
      const active = list.find(b => b.is_active) ?? list[0];
      if (active) { setBrandId(active.id); setBrandName(active.brand_name); }
    } catch {}
  }, []);

  useEffect(() => { loadTours(); loadBrandList(); }, [loadTours, loadBrandList]);

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

  function toggleTour(id: string, e: React.MouseEvent) {
    e.stopPropagation();
    setSelectedIds(prev => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  }

  function selectAll()   { setSelectedIds(new Set(filteredTours.map(t => t.tour_id))); }
  function deselectAll() { setSelectedIds(new Set()); }

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
            setToast(tRef.current("rewriteComplete", tourName));
            setTimeout(() => setToast(null), 5000);
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
  }, [tourStatuses]);

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
          <LoadingScreen msg={t("loadingTours")} />
        </PageFrame>
    );
  }

  return (
    <>
      <PageFrame variant="split" style={{ overflow: "hidden" }}>

        {/* ── Sticky Config Panel ─────────────────────────────────────────── */}
        <PageFrameHeader style={{
          position: "sticky", top: 0, zIndex: 20, background: A.card,
          padding: "12px 36px",
        }}>
          {/* S252 — flex-wrap, not a fixed 4-column grid: at phone width the three selects and the buttons stack. */}
          <div style={{ display: "flex", flexWrap: "wrap", gap: 12, alignItems: "flex-end" }}>
            <div style={{ flex: "1 1 200px", minWidth: 0 }}>
              <label style={{ fontSize: 11, color: A.muted, display: "block", marginBottom: 4 }}>{t("brandIdentity")}</label>
              <select
                value={brandId ?? ""}
                onChange={e => {
                  const id = e.target.value || null;
                  setBrandId(id);
                  const picked = brandList.find(b => b.id === id);
                  setBrandName(picked ? picked.brand_name : null);
                }}
                disabled={running}
                style={{ width: "100%", padding: "7px 10px", borderRadius: 6, border: `1px solid ${A.line}`, fontSize: 13, fontFamily: sans, background: A.card }}
              >
                {brandList.length === 0 && <option value="">{t("noBrand")}</option>}
                {brandList.map(b => (
                  <option key={b.id} value={b.id}>
                    {b.brand_name} · v{b.version}{b.is_active ? t("active") : ""}
                  </option>
                ))}
              </select>
            </div>
            <div style={{ flex: "1 1 200px", minWidth: 0 }}>
              <label style={{ fontSize: 11, color: A.muted, display: "block", marginBottom: 4 }}>{t("seoMode")}</label>
              <select
                value={seoMode}
                onChange={e => setSeoMode(e.target.value)}
                disabled={running}
                style={{ width: "100%", padding: "7px 10px", borderRadius: 6, border: `1px solid ${A.line}`, fontSize: 13, fontFamily: sans, background: A.card }}
              >
                <option value="standard">{t("seoStandardOption")}</option>
                <option value="aggressive">{t("seoAggressiveOption")}</option>
                <option value="minimal">{t("seoMinimalOption")}</option>
              </select>
            </div>
            <div style={{ flex: "1 1 200px", minWidth: 0 }}>
              <label style={{ fontSize: 11, color: A.muted, display: "block", marginBottom: 4 }}>{t("model")}</label>
              <div data-testid="s1-route-model"
                style={{ padding: "7px 0", fontSize: 13, color: A.ink, fontFamily: sans, overflowWrap: "anywhere" }}>
                {s1Model || "—"}{" "}
                <a href="/admin/ai-gateway?tab=routing" style={{ fontSize: 12, color: A.gold }}>{t("routeLink")}</a>
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
                  background: (running || selectedIds.size === 0) ? A.muted : A.gold,
                  border: `1px solid ${(running || selectedIds.size === 0) ? A.muted : A.gold}`,
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
                    color: seoPrefetch.state === "failed" ? A.red : seoPrefetch.state === "done" ? A.green : A.gold }}>
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
          <Card style={{ marginBottom: 16, padding: "12px 16px" }}>
            <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
              <select
                value={filterCountry}
                onChange={e => handleFilterChange(setFilterCountry, e.target.value)}
                style={{ padding: "6px 10px", borderRadius: 6, border: `1px solid ${A.line}`, fontSize: 13, fontFamily: sans, background: A.card, minWidth: 140 }}
              >
                <option value="">{t("allCountries")}</option>
                {uniqueCountries.map(c => <option key={c} value={c}>{c}</option>)}
              </select>

              <select
                value={filterFile}
                onChange={e => handleFilterChange(setFilterFile, e.target.value)}
                style={{ padding: "6px 10px", borderRadius: 6, border: `1px solid ${A.line}`, fontSize: 13, fontFamily: sans, background: A.card, minWidth: 180 }}
              >
                <option value="">{t("allFiles")}</option>
                {uniqueFiles.map(f => <option key={f} value={f}>{stripUuidPrefix(f)}</option>)}
              </select>

              <select
                value={filterStatus}
                onChange={e => handleFilterChange(setFilterStatus, e.target.value)}
                style={{ padding: "6px 10px", borderRadius: 6, border: `1px solid ${A.line}`, fontSize: 13, fontFamily: sans, background: A.card, minWidth: 130 }}
              >
                <option value="">{t("allStatus")}</option>
                <option value="published">{t("filterPublished")}</option>
                <option value="ready">{t("filterReady")}</option>
                <option value="review">{t("filterReview")}</option>
              </select>

              <div style={{ display: "flex", alignItems: "center", gap: 6, border: `1px solid ${A.line}`, borderRadius: 6, padding: "6px 10px", background: A.card, flex: 1, minWidth: 200 }}>
                <Search size={13} style={{ color: A.muted2, flexShrink: 0 }} />
                <input
                  placeholder={t("searchTour")}
                  value={filterSearch}
                  onChange={e => handleFilterChange(setFilterSearch, e.target.value)}
                  style={{ border: "none", outline: "none", fontSize: 13, fontFamily: sans, width: "100%", background: "transparent" }}
                />
              </div>

              <Button size="sm" variant="ghost" onClick={selectAll}>{t("selectAll", filteredTours.length)}</Button>
              <Button size="sm" variant="ghost" onClick={deselectAll} disabled={selectedIds.size === 0}>{t("deselectAll")}</Button>
            </div>
          </Card>

          {/* Tours table */}
          {tours.length === 0 ? (
            <Card style={{ textAlign: "center", padding: "48px 24px" }}>
              <div style={{ fontSize: 15, color: A.muted, marginBottom: 14 }}>{t("noTours")}</div>
              <div style={{ fontSize: 13, color: A.muted2, marginBottom: 20 }}>{t("uploadFirst")}</div>
              <Button size="sm" variant="secondary" onClick={() => window.location.href = "/admin/upload"}
                style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                {t("goToUpload")} <ArrowRight size={12} />
              </Button>
            </Card>
          ) : (
            <Card style={{ marginBottom: 20, padding: 0, overflow: "hidden" }}>
              <table style={{ width: "100%", borderCollapse: "collapse" }}>
                <thead style={{ position: "sticky", top: 0, zIndex: 2 }}>
                  <tr style={{ background: A.line2 }}>
                    <th style={{ ...TH, width: 36, paddingLeft: 16 }}>
                      <input
                        type="checkbox"
                        checked={filteredTours.length > 0 && filteredTours.every(t => selectedIds.has(t.tour_id))}
                        onChange={e => e.target.checked ? selectAll() : deselectAll()}
                        style={{ accentColor: A.gold }}
                      />
                    </th>
                    <th style={TH}>{t("colTourName")}</th>
                    <th style={TH}>{t("colCountry")}</th>
                    <th style={TH}>{t("colSourceFile")}</th>
                    <th style={TH}>{t("colRewrites")}</th>
                    <th style={TH}>{t("colLastRewritten")}</th>
                    <th style={TH}>{t("colIngested")}</th>
                    <th style={TH}>{t("colStatus")}</th>
                    <th style={{ ...TH, width: 28 }}></th>
                  </tr>
                </thead>
                <tbody>
                  {paginatedTours.map((t, i) => {
                    const runStatus = tourStatuses[t.tour_id] || "idle";
                    const result = runResults[t.tour_id];
                    const isSelected = selectedIds.has(t.tour_id);
                    const isDetail = detailTour?.tour_id === t.tour_id;
                    return (
                      <tr
                        key={t.tour_id}
                        onClick={() => setDetailTour(isDetail ? null : t)}
                        style={{
                          borderTop: i > 0 ? `1px solid ${A.line}` : undefined,
                          background: isDetail ? alpha(A.gold, 9) : isSelected ? alpha(A.gold, 6) : "transparent",
                          cursor: "pointer",
                          transition: "background .12s",
                        }}
                      >
                        <td style={{ ...TD, paddingLeft: 16 }} onClick={e => e.stopPropagation()}>
                          <input
                            type="checkbox"
                            checked={isSelected}
                            onChange={() => {}}
                            onClick={e => { e.stopPropagation(); setSelectedIds(prev => { const next = new Set(prev); next.has(t.tour_id) ? next.delete(t.tour_id) : next.add(t.tour_id); return next; }); }}
                            disabled={running}
                            style={{ accentColor: A.gold }}
                          />
                        </td>
                        <td style={{ ...TD, fontWeight: 500, color: A.ink }}>{t.src_name}</td>
                        <td style={TD}>
                          <span style={{ color: t.country ? A.body : A.muted2 }}>{t.country || "—"}</span>
                        </td>
                        <td style={TD}>
                          <span style={{ fontFamily: mono, fontSize: 11, color: A.muted }}>
                            {stripUuidPrefix(t.filename)}
                          </span>
                        </td>
                        <td style={{ ...TD, textAlign: "center" as const }}>
                          {t.rewrite_count > 0
                            ? <span style={{ fontFamily: mono, fontSize: 13, fontWeight: 600, color: A.ink }}>{t.rewrite_count}</span>
                            : <span style={{ color: A.muted2 }}>—</span>}
                        </td>
                        <td style={{ ...TD, color: A.muted2, fontSize: 12 }}>{relativeTime(t.last_rewritten_at, tr, formatDate)}</td>
                        <td style={{ ...TD, color: A.muted2, fontSize: 12 }}>{relativeTime(t.ingest_at, tr, formatDate)}</td>
                        <td style={TD}>
                          <PipelineStatusBadge
                            tour={t} runStatus={runStatus} result={result}
                            stage={tourStages[t.tour_id]} isRetry={!!tourRetryFlags[t.tour_id]}
                          />
                          {/* AA-667: open the live writing panel while this tour is running */}
                          {runStatus === "running" && jobIds[t.tour_id] && (
                            <button
                              onClick={(e) => {
                                e.stopPropagation();
                                setLiveTour({ tourId: t.tour_id, jobId: jobIds[t.tour_id], name: t.src_name });
                              }}
                              style={{
                                marginTop: 4, fontSize: 11, fontWeight: 600, color: A.gold,
                                background: "none", border: "none", cursor: "pointer", padding: 0,
                              }}
                            >
                              {tr("liveView")}
                            </button>
                          )}
                        </td>
                        <td style={{ ...TD, color: A.muted2 }}><ChevronRight size={14} /></td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              {filteredTours.length > PAGE_SIZE && (
                <div style={{ padding: "12px 20px", borderTop: `1px solid ${A.line}`, display: "flex", justifyContent: "flex-end" }}>
                  <Pagination page={page} total={filteredTours.length} pageSize={PAGE_SIZE} onPage={setPage} />
                </div>
              )}
            </Card>
          )}

          {/* Confirm dialog */}
          {showConfirm && (
            <div style={{
              position: "fixed", inset: 0, background: "rgba(0,0,0,0.45)",
              display: "flex", alignItems: "center", justifyContent: "center", zIndex: 100,
            }}>
              <Card style={{ maxWidth: 420, width: "90%", padding: 28 }}>
                <div style={{ fontFamily: serif, fontSize: 18, fontWeight: 500, color: A.ink, marginBottom: 12 }}>
                  {t("confirmTitle")}
                </div>
                <div style={{ fontSize: 14, color: A.body, marginBottom: 20, lineHeight: 1.6 }}>
                  {t("confirmLead")}{" "}
                  <strong>{t("toursCount", selectedIds.size)}</strong>{" "}
                  {t("using")} <strong>{t("seoModeName", seoModeLabel(seoMode))}</strong> {t("modeWith")}{" "}
                  <strong>{modelLabel()}</strong>
                  {brandName ? t("brandSuffix", brandName) : ""}{t("continueQ")}
                </div>
                <div style={{ display: "flex", gap: 10, justifyContent: "flex-end" }}>
                  <Button size="sm" variant="ghost" onClick={() => setShowConfirm(false)}>{t("cancel")}</Button>
                  <Button
                    size="sm"
                    variant="primary"
                    onClick={startRun}
                    style={{ background: A.gold, border: `1px solid ${A.gold}` }}
                  >
                    {t("yesRun")}
                  </Button>
                </div>
              </Card>
            </div>
          )}

          {/* AA-667: live writing panel (steps + streamed tour fields) for one running tour */}
          {liveTour && (
            <div
              onClick={() => setLiveTour(null)}
              style={{
                position: "fixed", inset: 0, background: "rgba(0,0,0,0.45)",
                display: "flex", justifyContent: "flex-end", zIndex: 120,
              }}
            >
              <div
                onClick={(e) => e.stopPropagation()}
                style={{
                  width: 480, maxWidth: "90vw", height: "100%", background: A.bg,
                  borderLeft: `1px solid ${A.line}`, padding: "22px 24px", overflowY: "auto",
                  boxShadow: "-8px 0 24px rgba(0,0,0,0.12)",
                }}
              >
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
                  <div style={{ fontFamily: serif, fontSize: 16, fontWeight: 500, color: A.ink, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {liveTour.name}
                  </div>
                  <button
                    onClick={() => setLiveTour(null)}
                    style={{ background: "none", border: "none", cursor: "pointer", color: A.muted, fontSize: 20, lineHeight: 1 }}
                    aria-label={t("close")}
                  >
                    ×
                  </button>
                </div>
                <AdminLiveWriter
                  jobId={liveTour.jobId}
                  active={tourStatuses[liveTour.tourId] === "running"}
                  title={t("title")}
                />
              </div>
            </div>
          )}

          {/* Progress summary */}
          {(running || runComplete) && (Object.keys(tourStatuses).length > 0 || runComplete) && (
            <Card style={{ marginBottom: 20 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 16, flexWrap: "wrap" }}>
                <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.14em", color: A.muted }}>
                  {t("progress")}
                </div>
                {Object.keys(tourStatuses).length > 0 && (
                  <span style={{ fontSize: 13, color: A.muted }}>
                    {t("complete", (statusCounts.done || 0) + (statusCounts.failed || 0), Object.keys(tourStatuses).length)}
                  </span>
                )}
                {(statusCounts.done || 0) > 0 && (
                  <span style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 13, color: A.green }}>
                    <CheckCircle size={13} /> {t("doneCount", statusCounts.done)}
                  </span>
                )}
                {(statusCounts.failed || 0) > 0 && (
                  <span style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 13, color: A.red }}>
                    <XCircle size={13} /> {t("failedCount", statusCounts.failed)}
                  </span>
                )}
                {running && <span style={{ fontSize: 13, color: A.amber }}>{t("processingEllipsis")}</span>}
                {runComplete && (
                  <Button
                    size="sm"
                    variant="primary"
                    onClick={() => window.location.href = "/admin/master-content"}
                    style={{
                      background: A.gold, border: `1px solid ${A.gold}`,
                      display: "flex", alignItems: "center", gap: 6, marginLeft: "auto",
                    }}
                  >
                    {t("viewMaster")} <ArrowRight size={12} />
                  </Button>
                )}
              </div>
            </Card>
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

      {/* Toast — bottom-right, auto-dismiss after 5s (AA-248) */}
      {toast && (
        <div style={{
          position: "fixed", bottom: 24, right: 28, zIndex: 9999,
          display: "flex", alignItems: "center", gap: 8,
          padding: "12px 18px", borderRadius: 10,
          background: A.card, border: `1px solid ${A.green}`,
          boxShadow: "0 4px 20px rgba(0,0,0,0.15)",
          fontSize: 13, fontWeight: 600, color: A.ink, fontFamily: sans,
          maxWidth: 380,
        }}>
          {toast}
        </div>
      )}
    </>
  );
}
