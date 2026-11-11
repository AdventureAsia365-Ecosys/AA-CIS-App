"use client";
// app/admin/ai-gateway/AiGateway.tsx — AA-804 (root ADR 0009): Platform › AI & spend › AI providers.
//
// The one page for every paid AI and data source. Tabs:
//   Providers        one row per paid source (ProvidersTab.tsx): health, spend today / this month, budget, balance;
//                    its views Connections (connection settings and test call) and Jev credit (the top-up ledger).
//   Routing          the model route of every stage (RoutingTab.tsx): primary, fallbacks, shadow, LLM availability.
//   Route control    AA-1042: order mode, presets, chain editor, provider order, preview, versions (RouteControlTab.tsx).
//   Models & trials  the model catalog (on / off; prices view-only) and the shadow comparisons (ModelsTab.tsx).
//   Budgets          every spending cap by tier (BudgetsTab.tsx).
//   Calls            every logged paid call, newest first, across the CIS ledgers and the CRM's (CallsTab.tsx).
//   Batches          AA-1076: the gateway batch lane: status, outcomes and spend per batch; send a wave, cancel one
//                    (BatchesTab.tsx).
//   Trace            AA-1042: how each stage's route behaved, every attempt of a request, the route alerts and the
//                    content a fallback model wrote (TraceTab.tsx); `?rt=<trace id>&rt_at=<time>` opens one request.
//   Guide            AA-1042: the gateway's reference page from the docs catalog (root docs/architecture/ai-gateway.md
//                    and its .vi.md) plus the live routes, matrix, connections, caps and coverage (GuideTab.tsx); the
//                    short page guide (guide.ts / guide.vi.ts) stays on top as "How to read this page".
// UI part 2 moved Routing, Models & trials, the connection settings and the Jev credit ledger here from Settings
// (LLM Models, Jev Credit) and Budgets from Cost Operations; the old links are sent on here by those pages
// (lib/aiGateway RETIRED_SETTINGS_TABS / RETIRED_COST_OPS_TABS).
// The Trace drawer (TraceDrawer.tsx) opens from `?trace=job:<id>` or `?trace=tour:<id>` (the Jobs drawer's and the
// tour panel's "Open trace" links, the decisions log, a Calls row); closing it drops the parameter. `?tab=` picks the tab and `?view=` the Providers view. They are read once on load and kept
// with history.replaceState, so the address always carries exactly one `tab`.
//
// Admin only (lib/rbac.ts PAGE_RULES and API_READ_RULES /admin/ai-gateway; backend spend.read for the reads, the
// writes keep their own capability: ai.admin for routes, catalog and connections, spend.write for budgets and Jev).

import { useQueryClient } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { useSearchParams } from "next/navigation";
import { useState } from "react";
import {
  Button, GuidePanel, LinkButton, PageFrame, PageHeader, Tabs, ToastProvider, useLocalized, useT,
} from "../../_kit";
import {
  parseTab, parseTraceTarget, parseView, traceParam, type ProviderView, type TabKey, type TraceTarget,
} from "@/lib/aiGateway";
import BatchesTab from "./BatchesTab";
import BudgetsTab from "./BudgetsTab";
import CallsTab from "./CallsTab";
import { AI_GATEWAY_GUIDE } from "./guide";
import { AI_GATEWAY_GUIDE_VI } from "./guide.vi";
import GuideTab from "./GuideTab";
import { M } from "./messages";
import TraceTab from "./TraceTab";
import { parseRouteTarget, type RouteTarget } from "@/lib/routeTrace";
import ModelsTab from "./ModelsTab";
import ProvidersTab from "./ProvidersTab";
import RouteControlTab from "./RouteControlTab";
import RoutingTab from "./RoutingTab";
import TraceDrawer from "./TraceDrawer";

/** Rewrite one query parameter in the address bar without a navigation (no re-render, no history entry). */
function setParam(name: string, value: string | null): void {
  try {
    const url = new URL(window.location.href);
    if (value == null) url.searchParams.delete(name);
    else url.searchParams.set(name, value);
    window.history.replaceState(window.history.state, "", `${url.pathname}${url.search}${url.hash}`);
  } catch {
    /* the address bar is a convenience: the page works without it */
  }
}

/** Every query key the page's tabs read, refreshed together by the header button. */
const PAGE_QUERIES = [
  ["ai-gateway"], ["llm-config"], ["llm-catalog"], ["llm-shadow-report"], ["llm-availability"], ["llm-connections"],
  ["llm-connection-costs"], ["jev-credit"], ["decisions-summary"], ["ai-gateway", "guide"], ["ai-gateway", "batches"],
] as const;

export default function AiGateway() {
  const t = useT(M);
  const qc = useQueryClient();
  const params = useSearchParams();
  const guide = useLocalized({ en: AI_GATEWAY_GUIDE, vi: AI_GATEWAY_GUIDE_VI });
  const [tab, setTabState] = useState<TabKey>(() => parseTab(params.get("tab")));
  const [view, setViewState] = useState<ProviderView>(() => parseView(params.get("view")));
  const [trace, setTraceState] = useState<TraceTarget | null>(() => parseTraceTarget(params.get("trace")));
  const [route, setRouteState] = useState<RouteTarget | null>(
    () => parseRouteTarget(params.get("rt"), params.get("rt_at")));
  const tabs = [
    { key: "providers", label: t("tabProviders") },
    { key: "routing", label: t("tabRouting") },
    { key: "controls", label: t("tabRouteControl") },
    { key: "models", label: t("tabModels") },
    { key: "budgets", label: t("tabBudgets") },
    { key: "calls", label: t("tabCalls") },
    { key: "batches", label: t("tabBatches") },
    { key: "trace", label: t("tabTrace") },
    { key: "guide", label: t("tabGuide") },
  ];

  const setTab = (k: string) => {
    const next = parseTab(k);
    setTabState(next);
    setParam("tab", next === "providers" ? null : next);
    if (next !== "providers") setParam("view", null);
    else setParam("view", view === "overview" ? null : view);
  };
  const setView = (v: ProviderView) => {
    setViewState(v);
    setParam("view", v === "overview" ? null : v);
  };
  const openTrace = (target: TraceTarget) => {
    setTraceState(target);
    setParam("trace", traceParam(target));
  };
  const openRoute = (target: RouteTarget | null) => {
    setRouteState(target);
    setParam("rt", target?.id ?? null);
    setParam("rt_at", target?.at ?? null);
  };
  const openRouteTab = (target: RouteTarget) => {
    setTab("trace");
    openRoute(target);
  };
  const closeTrace = () => {
    setTraceState(null);
    setParam("trace", null);
  };

  return (
    <ToastProvider>
      <PageFrame>
        <PageHeader title={t("title")}
          breadcrumbs={[{ label: t("crumbGroup") }, { label: t("title") }]}
          description={<>{t("description")}{" "}
            <span data-testid="ai-gateway-guide-link" style={{ display: "inline-flex" }}>
              <LinkButton onClick={() => setTab("guide")}>{t("readGuide")}</LinkButton></span></>}
          actions={
            <Button variant="ghost"
              onClick={() => PAGE_QUERIES.forEach((queryKey) => qc.invalidateQueries({ queryKey: [...queryKey] }))}>
              <RefreshCw size={14} /> {t("refresh")}</Button>
          } />
        <div style={{ marginBottom: 14 }} data-testid="ai-gateway-tabs">
          <Tabs tabs={tabs} active={tab} onChange={setTab} />
        </div>
        {tab === "providers" && <ProvidersTab view={view} onView={setView} />}
        {tab === "routing" && <RoutingTab />}
        {tab === "controls" && <RouteControlTab />}
        {tab === "models" && <ModelsTab />}
        {tab === "budgets" && <BudgetsTab onOpenConnections={() => { setTab("providers"); setView("connections"); }} />}
        {tab === "calls" && <CallsTab onTrace={openTrace} onRoute={openRouteTab} />}
        {tab === "batches" && <BatchesTab />}
        {tab === "trace" && <TraceTab target={route} onOpen={openRoute} />}
        {tab === "guide" && (
          <GuideTab howToRead={<GuidePanel content={guide} testId="ai-gateway-guide-panel" />} />
        )}
        {trace && <TraceDrawer key={traceParam(trace)} target={trace} onClose={closeTrace} />}
      </PageFrame>
    </ToastProvider>
  );
}
