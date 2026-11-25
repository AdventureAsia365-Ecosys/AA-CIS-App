"use client";
// app/admin/overview/page.tsx
// AA-1140 (S289) — Ecosystem status: one live and period status page for CIS, TripPlanner, Booking and the partner
// CRM (S285 advisory, mockup v2 approved by the tech lead 19/11). It replaces the old Overview (AA-664 / AA-858):
// the content pipeline funnel and content counts moved to Data coverage (data-coverage/ContentPipeline.tsx), tenant
// activity and quotas to Tenants (tenants/TenantActivity.tsx), cost per app and the paid sources to Spend (its By app
// and AI & data spend tabs already show them, from the same cost module).
//
// Data: ONE call, GET /api/admin/overview/status?range=&from=&to=&compare= (backend api/services/ops_status/, ops.read
// = admin): every section carries its own as_of and degrades on its own; a signal with no source is "not measured
// yet", never green; every limit drawn comes from `thresholds` (cis.ops.status_* settings, Platform › Settings).
// The view (time range, compare, app / severity / status filters, legends, sorts) lives in the URL
// (lib/opsStatus.ts), so "Copy link to this view" shares it. A preset range refreshes every minute; a custom one does
// not move. Kit only (ADR 0016); English by default, Vietnamese in status.messages.ts.
//
// AA-1141 (P1, S291): change annotations on every chart and a What changed panel, ack / snooze / mute with a reason
// (viewer.can_ack), "as of" chips that grey out when stale plus the background producers, a capacity forecast, the AI
// cost anomaly banner, the OPS-12 digest from the same verdict, named saved views, auto refresh paused on a hidden
// tab / custom or zoomed range / by hand, and WCAG 2.2 AA (levels in words, table views, aria-live verdict).
//
// AA-1142 (P2, S292): availability and error budget burn per app (sections.slo), the external checks strip next to the
// internal services (sections.external), the incident record with its timeline, and the per-agency drill-down.

import { useQuery } from "@tanstack/react-query";
import { Suspense, useEffect, useState, useSyncExternalStore } from "react";
import {
  Button, ErrorState, LoadingScreen, PageFrame, PageHeader, ToastProvider, apiGet, useFormat, useUrlFilters,
} from "../../_kit";
import { usePathname } from "next/navigation";
import {
  RESET_VIEW, nextViewQuery, parseView, refreshPause, resetViewQuery, statusQuery, type SetView, type StatusResp,
  type View,
} from "@/lib/opsStatus";
import { STATUS_QUERY_KEY } from "./_status/Ack";
import { AnomalyBanner, ForecastCard } from "./_status/Capacity";
import { ChangesCard } from "./_status/Changes";
import { RefreshChip } from "./_status/Views";
import { AppsCard } from "./_status/Apps";
import { AiCard, CostCard, JobsCard, ResourcesCard } from "./_status/Panels";
import { ServicesCard } from "./_status/Services";
import { SignalsCard } from "./_status/Signals";
import { DeliveryCard, DrillLinks, EventsCard, SourcesCard } from "./_status/Timeline";
import { Toolbar } from "./_status/Toolbar";
import { VerdictCard } from "./_status/Verdict";
// AA-1142 (P2): availability and error budget, external checks, incidents, by agency
import { AgenciesCard } from "./_status/Agencies";
import { ExternalCard } from "./_status/External";
import { IncidentsCard } from "./_status/Incidents";
import { SloCard } from "./_status/Slo";
import { p2Sections } from "@/lib/opsStatusP2";
import { Freshness, small, useST } from "./_status/ui";

const REFRESH_MS = 60_000; // technical-constant: page refresh of a preset range (the server caches its AWS reads)

/** The view in the URL. Reads come from the search params of this render; every write starts from the URL as it is at
 *  that moment (window.location) and goes through the native history.replaceState, which Next.js syncs into
 *  useSearchParams at once (no server round trip). A router.replace only updates the URL after its navigation, so two
 *  quick clicks both started from the old URL and the first was lost. */
function useView(): { view: View; setView: SetView; reset: () => void; dirty: boolean; apply: (qs: string) => void } {
  const { get, params } = useUrlFilters();
  const pathname = usePathname();
  const view = parseView(get);
  const dirty = Object.keys(RESET_VIEW).some((k) => params.has(k));
  const write = (qs: string) => window.history.replaceState(null, "", qs ? `${pathname}?${qs}` : pathname);
  return {
    view, dirty,
    setView: (change) => write(nextViewQuery(window.location.search, change)),
    reset: () => write(resetViewQuery(window.location.search)),
    apply: write,
  };
}

/** AA-1141: is the tab hidden (document.visibilityState)? Auto refresh waits while it is. */
function subscribeVisibility(cb: () => void): () => void {
  document.addEventListener("visibilitychange", cb);
  return () => document.removeEventListener("visibilitychange", cb);
}
function useTabHidden(): boolean {
  return useSyncExternalStore(subscribeVisibility, () => document.visibilityState === "hidden", () => false);
}

/** cis.ops.status_history_days from the last answer (unknown before the first one). */
function historyDaysOf(data: StatusResp | undefined): number | null {
  return data?.thresholds.history_days?.value ?? null;
}

function Countdown({ updatedAt, live }: { updatedAt: number; live: boolean }) {
  const t = useST();
  const { formatDateTime } = useFormat();
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);
  if (!updatedAt) return null;
  const left = Math.max(0, Math.round((updatedAt + REFRESH_MS - now) / 1000));
  return (
    <span style={small} data-testid="status-updated">
      {t("updated", formatDateTime(updatedAt))}{live ? ` · ${t("nextIn", left)}` : ""}
    </span>
  );
}

function Body({ data, view, setView }: { data: StatusResp; view: View; setView: SetView }) {
  const s = data.sections;
  const grid2 = { display: "grid", gap: 18, gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 460px), 1fr))" };
  const fresh = { factor: data.thresholds.stale_factor?.value ?? null, nowMs: Date.parse(data.generated_at) };
  const p2 = p2Sections(data);
  return (
    <Freshness.Provider value={fresh}>
    <div style={{ display: "grid", gap: 18, gridTemplateColumns: "minmax(0, 1fr)" }} data-testid="status-body">
      <AnomalyBanner section={s.anomaly} view={view} />
      <VerdictCard verdict={data.verdict} view={view} setView={setView} rules={data.acks?.rules}
        canAck={!!data.viewer?.can_ack} trail={data.acks?.recent ?? []} />

      <IncidentsCard attention={data.verdict.attention} canWrite={!!data.viewer?.can_ack} range={data.range.key} />
      <AppsCard section={s.apps} sig={s.signals} view={view} />

      <SloCard section={p2.slo} view={view} />
      <SignalsCard data={data} view={view} setView={setView} />
      <ChangesCard section={s.changes} view={view} setView={setView} />
      <ServicesCard section={s.runtimes} view={view} setView={setView} />

      <ExternalCard section={p2.external} view={view} />
      <div style={grid2}>
        <JobsCard section={s.jobs} view={view} setView={setView} />
        <ResourcesCard section={s.resources} />
      </div>
      <div style={grid2}>
        <CostCard section={s.cost} />
        <DeliveryCard section={s.delivery} />
      </div>
      <AiCard section={s.ai} view={view} setView={setView} />

      <AgenciesCard view={view} />
      <ForecastCard section={s.forecast} />
      <EventsCard section={s.events} range={data.range} view={view} setView={setView} />
      <SourcesCard rows={data.sources} staleFactor={data.thresholds.stale_factor?.value ?? null}
        pipelines={data.pipelines ?? []} />
      <DrillLinks />
    </div>
    </Freshness.Provider>
  );
}

function Content({ res, view, setView }: {
  res: { data?: StatusResp; isLoading: boolean; isError: boolean; error: unknown; refetch: () => unknown };
  view: View; setView: SetView;
}) {
  const t = useST();
  if (res.isLoading) return <LoadingScreen message={t("loading")} />;
  if (!res.data) {
    return res.isError
      ? <ErrorState message={(res.error as Error)?.message || t("loadFailed")} onRetry={() => res.refetch()} />
      : null;
  }
  return <Body data={res.data} view={view} setView={setView} />;
}

function StatusPage() {
  const t = useST();
  const { view, setView, reset, dirty, apply } = useView();
  // AA-1141: auto refresh pauses on a custom (or zoomed) range, while the tab is hidden, or by hand
  const hidden = useTabHidden();
  const [manual, setManual] = useState(false);
  const pause = refreshPause(view, hidden, manual);
  const live = pause === null;
  const q = statusQuery(view);
  const res = useQuery({
    queryKey: [STATUS_QUERY_KEY, q],
    queryFn: () => apiGet<StatusResp>(`/api/admin/overview/status?${q}`, { admin: true }),
    refetchInterval: live ? REFRESH_MS : false,
    refetchOnWindowFocus: false,
    placeholderData: (prev) => prev,
  });
  const data = res.data;
  return (
    <PageFrame>
      <PageHeader title={t("title")} description={t("description")}
        actions={<span style={{ display: "inline-flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
          <Countdown updatedAt={res.dataUpdatedAt} live={live} />
          <Button variant="ghost" onClick={() => res.refetch()}>{res.isFetching ? t("refreshing") : t("refresh")}</Button>
        </span>} />
      <Toolbar view={view} setView={setView} range={data?.range} dirty={dirty} onReset={reset} applyQuery={apply}
        historyDays={historyDaysOf(data)}
        refresh={<RefreshChip pause={pause} seconds={REFRESH_MS / 1000} onToggle={() => setManual((m) => !m)} />} />
      <div style={{ height: 14 }} />
      <Content res={res} view={view} setView={setView} />
    </PageFrame>
  );
}

export default function OverviewPage() {
  // useSearchParams (the view in the URL) needs a Suspense boundary for `next build`.
  return (
    // AA-1141: the ack and saved-view controls raise toasts, so the page needs the kit's ToastProvider.
    <ToastProvider>
      <Suspense fallback={null}>
        <StatusPage />
      </Suspense>
    </ToastProvider>
  );
}
