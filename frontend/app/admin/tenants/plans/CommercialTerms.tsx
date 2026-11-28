"use client";
// app/admin/tenants/plans/CommercialTerms.tsx — AA-1116 (option B): the commercial terms in force (annual months,
// founding offer, combo, reference rate, review periods, trials, pilot) read-only with a link to Platform › Settings,
// where they are edited with a second person's approval, and the price review dates of every priced plan with the
// VND drift against the reference rate. GET /api/admin/plans/commercial-terms through the BFF. AA-1191: while VND is
// not an enabled currency the API leaves the VND terms out and the VND review / drift columns are hidden.

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { Badge, DataTable, ErrorState, K, Section, mono, useT, type ColumnDef } from "../../../_kit";
import {
  COMMERCIAL_TERMS_API, currencyEnabled, sendJson, termText, type CommercialTerm, type CommercialTermsResp,
  type PlanReview,
} from "@/lib/tenantPlans";
import { PLANS } from "./plans.messages";

export function CommercialTerms() {
  const t = useT(PLANS);
  const q = useQuery({
    queryKey: ["admin-plans", "commercial-terms"],
    queryFn: () => sendJson<CommercialTermsResp>(COMMERCIAL_TERMS_API, "GET"),
  });
  const termCols: ColumnDef<CommercialTerm, unknown>[] = [
    {
      accessorKey: "label", header: t("colTerm"),
      cell: (c) => (
        <span style={{ display: "flex", flexDirection: "column", minWidth: 0 }}>
          <span style={{ color: K.ink }}>{c.row.original.label}</span>
          <span style={{ fontSize: 11, color: K.muted, fontFamily: mono }}>{c.row.original.key}</span>
        </span>
      ),
    },
    { id: "value", header: t("colValue"), cell: (c) => termText(c.row.original.value, t("termOn"), t("termOff")) },
  ];
  const due = (date: string | null, overdue: boolean) => !date ? "—" : (
    <span style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
      {date}{overdue && <Badge tone="warning">{t("overdue")}</Badge>}
    </span>
  );
  const vnd = currencyEnabled("VND", q.data?.enabled_currencies);
  const reviewCols: ColumnDef<PlanReview, unknown>[] = [
    { accessorKey: "name", header: t("colPlan") },
    { id: "review", header: t("colReviewDue"), cell: (c) => due(c.row.original.price_review_due,
      c.row.original.price_review_overdue) },
    ...(vnd ? [
      { id: "vnd", header: t("colVndReview"), cell: (c) => due(c.row.original.vnd_review_due,
        c.row.original.vnd_review_overdue) },
      { id: "drift", header: t("colDrift"),
        cell: (c) => c.row.original.vnd_drift_pct == null ? "—" : `${c.row.original.vnd_drift_pct} %` },
    ] satisfies ColumnDef<PlanReview, unknown>[] : []),
  ];
  if (q.error) return <ErrorState message={`${t("loadError")}: ${(q.error as Error).message}`} />;
  return (
    <div data-testid="commercial-terms" style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 420px), 1fr))",
      gap: 16, marginBottom: 16 }}>
      <Section title={t("termsTitle")} description={t("termsHint")} padded={false}
        actions={<Link href="/admin/platform/settings?app=cis&area=billing" style={{ fontSize: 13 }}>{t("termsLink")}</Link>}>
        <DataTable<CommercialTerm> tableId="commercial-terms" data={q.data?.terms ?? []} columns={termCols}
          getRowId={(r) => r.key} loading={q.isLoading} paginate={false} compact tableMinWidth={360}
          emptyTitle="—" />
      </Section>
      <Section title={t("reviewsTitle")} padded={false}>
        <DataTable<PlanReview> tableId="plan-reviews" data={q.data?.reviews ?? []} columns={reviewCols}
          getRowId={(r) => r.plan_key} loading={q.isLoading} paginate={false} compact tableMinWidth={420}
          emptyTitle={t("noReviews")} />
      </Section>
    </div>
  );
}
