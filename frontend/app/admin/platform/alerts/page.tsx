"use client";
// app/admin/platform/alerts/page.tsx — Platform › Alerts: the Catalogue (AA-1171, every alert condition, read only) and
// the Recipients (AA-913, who gets the alert e-mails) tabs. The tab is in the URL (?tab=catalogue | recipients), so the
// Ecosystem status card links straight to the Catalogue. Admin only (lib/rbac.ts PAGE_RULES /admin/platform; backend
// ops.read / alerts.write).
import { Suspense } from "react";
import { PageFrame, Tabs, ToastProvider, useT, useUrlFilters } from "../../../_kit";
import AlertRecipients from "./AlertRecipients";
import AlertsCatalogue from "./AlertsCatalogue";
import { CATALOGUE } from "./catalogue.messages";

type Tab = "catalogue" | "recipients";

function AlertsTabs() {
  const t = useT(CATALOGUE);
  const { get, setMany } = useUrlFilters();
  const tab: Tab = get("tab") === "recipients" ? "recipients" : "catalogue";
  return (
    <PageFrame>
      <div style={{ marginBottom: 16 }}>
        <Tabs ariaLabel={t("pageTitle")} active={tab} testId="alerts-tabs" variant="underline"
          onChange={(next) => setMany({ tab: next === "catalogue" ? null : next, q: null, app: null, type: null,
            severity: null, state: null, edit: null, gaps: null })}
          tabs={[{ key: "catalogue", label: t("tabCatalogue") }, { key: "recipients", label: t("tabRecipients") }]} />
      </div>
      {tab === "catalogue" ? <AlertsCatalogue /> : <AlertRecipients />}
    </PageFrame>
  );
}

export default function PlatformAlertsPage() {
  return (
    <ToastProvider>
      <Suspense fallback={null}>
        <AlertsTabs />
      </Suspense>
    </ToastProvider>
  );
}
