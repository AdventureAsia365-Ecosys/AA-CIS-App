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

import { useQuery } from "@tanstack/react-query";
import { Suspense, useEffect, useState } from "react";
import { Button, ErrorState, LoadingScreen, PageFrame, PageHeader, apiGet, useFormat, useUrlFilters } from "../../_kit";
import { RESET_VIEW, parseView, statusQuery, viewParams, type StatusResp, type View } from "@/lib/opsStatus";
import { AppsCard } from "./_status/Apps";
import { AiCard, CostCard, JobsCard, ResourcesCard } from "./_status/Panels";
import { ServicesCard } from "./_status/Services";
import { SignalsCard } from "./_status/Signals";
import { DeliveryCard, DrillLinks, EventsCard, SourcesCard } from "./_status/Timeline";
import { Toolbar } from "./_status/Toolbar";
import { VerdictCard } from "./_status/Verdict";
import { small, useST } from "./_status/ui";

const REFRESH_MS = 60_000; // technical-constant: page refresh of a preset range (the server caches its AWS reads)

function useView(): { view: View; setView: (v: Partial<View>) => void; reset: () => void; dirty: boolean } {
  const { get, setMany, params } = useUrlFilters();
  const view = parseView(get);
  const dirty = Object.keys(RESET_VIEW).some((k) => params.has(k));
  return { view, setView: (v) => setMany(viewParams(v)), reset: () => setMany(RESET_VIEW), dirty };
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

function Body({ data, view, setView }: { data: StatusResp; view: View; setView: (v: Partial<View>) => void }) {
  const s = data.sections;
  const grid2 = { display: "grid", gap: 18, gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 460px), 1fr))" };
  return (
    <div style={{ display: "grid", gap: 18, gridTemplateColumns: "minmax(0, 1fr)" }} data-testid="status-body">
      <VerdictCard verdict={data.verdict} view={view} setView={setView} />
      <AppsCard section={s.apps} sig={s.signals} view={view} />
      <SignalsCard data={data} view={view} setView={setView} />
      <ServicesCard section={s.runtimes} view={view} setView={setView} />
      <div style={grid2}>
        <JobsCard section={s.jobs} view={view} setView={setView} />
        <ResourcesCard section={s.resources} />
      </div>
      <div style={grid2}>
        <CostCard section={s.cost} />
        <DeliveryCard section={s.delivery} />
      </div>
      <AiCard section={s.ai} view={view} setView={setView} />
      <EventsCard section={s.events} range={data.range} view={view} setView={setView} />
      <SourcesCard rows={data.sources} staleFactor={data.thresholds.stale_factor?.value ?? null} />
      <DrillLinks />
    </div>
  );
}

function Content({ res, view, setView }: {
  res: { data?: StatusResp; isLoading: boolean; isError: boolean; error: unknown; refetch: () => unknown };
  view: View; setView: (v: Partial<View>) => void;
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
  const { view, setView, reset, dirty } = useView();
  const live = view.range !== "custom";
  const q = statusQuery(view);
  const res = useQuery({
    queryKey: ["admin-ecosystem-status", q],
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
      <Toolbar view={view} setView={setView} range={data?.range} dirty={dirty} onReset={reset}
        historyDays={historyDaysOf(data)} />
      <div style={{ height: 14 }} />
      <Content res={res} view={view} setView={setView} />
    </PageFrame>
  );
}

export default function OverviewPage() {
  // useSearchParams (the view in the URL) needs a Suspense boundary for `next build`.
  return (
    <Suspense fallback={null}>
      <StatusPage />
    </Suspense>
  );
}
