"use client";
// app/admin/ai-gateway/AiGateway.tsx — AA-804 (UI part 1, root ADR 0009): Platform › AI & spend › AI providers.
//
// The one page for every paid AI and data source. Tabs today:
//   Providers   one row per paid source (ProvidersTab.tsx): health, spend today / this month, budget, balance.
//   Guide       how to read the page (guide.ts / guide.vi.ts), the kit GuidePanel like every admin guide.
// More tabs (Routing, Models & trials, Budgets, Calls) come with the registry and ledger work; until then the
// existing Settings › LLM Models and Cost Operations pages keep their editors.
// The Trace drawer (TraceDrawer.tsx) opens from `?trace=job:<id>` (the Jobs drawer's "Open trace" link); closing it
// drops the parameter. `?tab=` picks the tab. Both are read once on load and kept with history.replaceState.
//
// Admin only (lib/rbac.ts PAGE_RULES and API_READ_RULES /admin/ai-gateway; backend spend.read). Read-only, $0.

import { useQueryClient } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { useSearchParams } from "next/navigation";
import { useState } from "react";
import { Button, GuidePanel, LinkButton, PageFrame, PageHeader, Tabs, useLocalized, useT } from "../../_kit";
import { parseTrace } from "@/lib/aiGateway";
import { AI_GATEWAY_GUIDE } from "./guide";
import { AI_GATEWAY_GUIDE_VI } from "./guide.vi";
import { M } from "./messages";
import ProvidersTab from "./ProvidersTab";
import TraceDrawer from "./TraceDrawer";

const TAB_KEYS = ["providers", "guide"] as const;
type TabKey = (typeof TAB_KEYS)[number];

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

export default function AiGateway() {
  const t = useT(M);
  const qc = useQueryClient();
  const params = useSearchParams();
  const guide = useLocalized({ en: AI_GATEWAY_GUIDE, vi: AI_GATEWAY_GUIDE_VI });
  const [tab, setTabState] = useState<TabKey>(() => {
    const wanted = params.get("tab");
    return (TAB_KEYS as readonly string[]).includes(wanted ?? "") ? (wanted as TabKey) : "providers";
  });
  const [traceJob, setTraceJob] = useState<string | null>(() => parseTrace(params.get("trace")));
  const tabs = [{ key: "providers", label: t("tabProviders") }, { key: "guide", label: t("tabGuide") }];

  const setTab = (k: string) => {
    const next = (TAB_KEYS as readonly string[]).includes(k) ? (k as TabKey) : "providers";
    setTabState(next);
    setParam("tab", next === "providers" ? null : next);
  };
  const closeTrace = () => {
    setTraceJob(null);
    setParam("trace", null);
  };

  return (
    <PageFrame>
      <PageHeader title={t("title")}
        breadcrumbs={[{ label: t("crumbPlatform") }, { label: t("crumbGroup") }, { label: t("title") }]}
        description={<>{t("description")}{" "}
          <span data-testid="ai-gateway-guide-link" style={{ display: "inline-flex" }}>
            <LinkButton onClick={() => setTab("guide")}>{t("readGuide")}</LinkButton></span></>}
        actions={
          <Button variant="ghost" onClick={() => qc.invalidateQueries({ queryKey: ["ai-gateway"] })}>
            <RefreshCw size={14} /> {t("refresh")}</Button>
        } />
      <div style={{ marginBottom: 14 }}><Tabs tabs={tabs} active={tab} onChange={setTab} /></div>
      {tab === "providers" ? <ProvidersTab /> : <GuidePanel content={guide} testId="ai-gateway-guide" />}
      {traceJob && <TraceDrawer jobId={traceJob} onClose={closeTrace} />}
    </PageFrame>
  );
}
