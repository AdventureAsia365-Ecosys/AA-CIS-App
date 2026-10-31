"use client";
// app/admin/ai-gateway/AiGateway.tsx — AA-804 (root ADR 0009): Platform › AI & spend › AI providers.
//
// The one page for every paid AI and data source. Tabs:
//   Providers        one row per paid source (ProvidersTab.tsx): health, spend today / this month, budget, balance;
//                    its views Connections (connection settings and test call) and Jev credit (the top-up ledger).
//   Routing          the model route of every stage (RoutingTab.tsx): primary, fallbacks, shadow, LLM availability.
//   Models & trials  the model catalog (on / off; prices view-only) and the shadow comparisons (ModelsTab.tsx).
//   Budgets          every spending cap by tier (BudgetsTab.tsx).
//   Guide            how to read the page (guide.ts / guide.vi.ts), the kit GuidePanel like every admin guide.
// UI part 2 moved Routing, Models & trials, the connection settings and the Jev credit ledger here from Settings
// (LLM Models, Jev Credit) and Budgets from Cost Operations; the old links are sent on here by those pages
// (lib/aiGateway RETIRED_SETTINGS_TABS / RETIRED_COST_OPS_TABS).
// The Trace drawer (TraceDrawer.tsx) opens from `?trace=job:<id>` (the Jobs drawer's "Open trace" link); closing it
// drops the parameter. `?tab=` picks the tab and `?view=` the Providers view. They are read once on load and kept
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
import { parseTab, parseTrace, parseView, type ProviderView, type TabKey } from "@/lib/aiGateway";
import BudgetsTab from "./BudgetsTab";
import { AI_GATEWAY_GUIDE } from "./guide";
import { AI_GATEWAY_GUIDE_VI } from "./guide.vi";
import { M } from "./messages";
import ModelsTab from "./ModelsTab";
import ProvidersTab from "./ProvidersTab";
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
  ["llm-connection-costs"], ["jev-credit"], ["decisions-summary"],
] as const;

export default function AiGateway() {
  const t = useT(M);
  const qc = useQueryClient();
  const params = useSearchParams();
  const guide = useLocalized({ en: AI_GATEWAY_GUIDE, vi: AI_GATEWAY_GUIDE_VI });
  const [tab, setTabState] = useState<TabKey>(() => parseTab(params.get("tab")));
  const [view, setViewState] = useState<ProviderView>(() => parseView(params.get("view")));
  const [traceJob, setTraceJob] = useState<string | null>(() => parseTrace(params.get("trace")));
  const tabs = [
    { key: "providers", label: t("tabProviders") },
    { key: "routing", label: t("tabRouting") },
    { key: "models", label: t("tabModels") },
    { key: "budgets", label: t("tabBudgets") },
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
  const closeTrace = () => {
    setTraceJob(null);
    setParam("trace", null);
  };

  return (
    <ToastProvider>
      <PageFrame>
        <PageHeader title={t("title")}
          breadcrumbs={[{ label: t("crumbPlatform") }, { label: t("crumbGroup") }, { label: t("title") }]}
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
        {tab === "models" && <ModelsTab />}
        {tab === "budgets" && <BudgetsTab onOpenConnections={() => { setTab("providers"); setView("connections"); }} />}
        {tab === "guide" && <GuidePanel content={guide} testId="ai-gateway-guide" />}
        {traceJob && <TraceDrawer jobId={traceJob} onClose={closeTrace} />}
      </PageFrame>
    </ToastProvider>
  );
}
