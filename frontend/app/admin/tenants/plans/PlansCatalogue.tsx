"use client";
// app/admin/tenants/plans/PlansCatalogue.tsx — AA-1028 R1 slice 3 (root ADR 0019 d2): the plan catalogue, one kit
// DataTable per product (Content, CRM add-on, TripPlanner add-on). A row opens PlanDrawer (edit / retire); "New plan"
// opens it empty. GET /api/admin/plans (+ include_retired) and /api/admin/plans/features through the BFF.

import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, RefreshCw } from "lucide-react";
import {
  Badge, Button, DataTable, ErrorState, K, PageHeader, Section, Switch, mono, useT, type ColumnDef,
} from "../../../_kit";
import {
  FEATURES_API, PLANS_API, PLANS_KEY, PRODUCTS, sendJson, type Feature, type Plan, type PlansResp, type Product,
} from "@/lib/tenantPlans";
import { PLANS } from "./plans.messages";
import { PlanDrawer } from "./PlanDrawer";

export default function PlansCatalogue() {
  const t = useT(PLANS);
  const qc = useQueryClient();
  const [showRetired, setShowRetired] = useState(false);
  const [open, setOpen] = useState<{ plan: Plan | null; product: Product } | null>(null);
  const plansQ = useQuery({
    queryKey: [...PLANS_KEY, showRetired],
    queryFn: () => sendJson<PlansResp>(`${PLANS_API}${showRetired ? "?include_retired=true" : ""}`, "GET"),
  });
  const featuresQ = useQuery({
    queryKey: [...PLANS_KEY, "features"],
    queryFn: () => sendJson<{ features: Feature[] }>(FEATURES_API, "GET").then((r) => r.features),
    staleTime: 5 * 60_000,
  });
  const catalogue = useMemo(() => featuresQ.data ?? [], [featuresQ.data]);

  const columns = useMemo<ColumnDef<Plan, unknown>[]>(() => [
    {
      accessorKey: "name", header: t("colPlan"),
      cell: (c) => (
        <span style={{ display: "flex", flexDirection: "column", minWidth: 0 }}>
          <span style={{ fontWeight: 600, color: K.ink }}>{c.row.original.name}</span>
          <span style={{ fontSize: 11, color: K.muted, fontFamily: mono }}>{c.row.original.plan_key}</span>
        </span>
      ),
    },
    { accessorKey: "tier", header: t("colTier"), cell: (c) => c.row.original.tier ?? t("noTier") },
    {
      accessorKey: "price_usd_monthly", header: t("colPrice"),
      cell: (c) => {
        const p = c.row.original;
        return p.price_usd_monthly == null ? <span style={{ color: K.muted }}>{t("noPrice")}</span>
          : t("perUnit", p.price_usd_monthly, t(`unit_${p.price_unit}`));
      },
    },
    { id: "features", header: t("colFeatures"), cell: (c) => t("featureCount", c.row.original.features.length) },
    { accessorKey: "live_subscriptions", header: t("colSubscribers") },
    { accessorKey: "version", header: t("colVersion") },
    {
      id: "state", header: t("colState"),
      cell: (c) => (
        <span style={{ display: "inline-flex", gap: 6, flexWrap: "wrap" }}>
          {c.row.original.retired_at ? <Badge tone="danger">{t("retired")}</Badge> : <Badge tone="success">{t("live")}</Badge>}
          {!c.row.original.is_public && !c.row.original.retired_at && <Badge tone="neutral" dot={false}>{t("hidden")}</Badge>}
        </span>
      ),
    },
  ], [t]);

  const refresh = () => { void qc.invalidateQueries({ queryKey: PLANS_KEY }); };
  const error = plansQ.error ? (plansQ.error as Error).message : featuresQ.error ? (featuresQ.error as Error).message : null;
  const byProduct = (p: Product) => (plansQ.data?.plans ?? []).filter((x) => x.product === p);

  return (
    <>
      <PageHeader
        title={t("title")}
        breadcrumbs={[{ label: t("crumbTenants"), href: "/admin/tenants" }, { label: t("title") }]}
        description={t("description")}
        actions={
          <>
            <Button variant="secondary" size="sm" onClick={refresh} disabled={plansQ.isFetching}>
              <RefreshCw size={13} /> {plansQ.isFetching ? t("loading") : t("refresh")}
            </Button>
            <Button variant="primary" size="sm" disabled={!featuresQ.data} testId="plan-new"
              onClick={() => setOpen({ plan: null, product: "content" })}>
              <Plus size={13} /> {t("newPlan")}
            </Button>
          </>
        }
      />
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 14 }}>
        <Switch checked={showRetired} onChange={setShowRetired} label={t("showRetired")} />
        <span style={{ fontSize: 13, color: K.body }}>{t("showRetired")}</span>
      </div>
      {error ? <ErrorState message={`${t("loadError")}: ${error}`} onRetry={refresh} /> : (
        <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 16 }}>
          {PRODUCTS.map((p) => (
            <Section key={p} title={t(`product_${p}`)} padded={false} testId={`plans-${p}`}>
              <DataTable<Plan>
                tableId={`plans-${p}`}
                data={byProduct(p)}
                columns={columns}
                getRowId={(r) => r.plan_key}
                loading={plansQ.isLoading}
                paginate={false}
                emptyTitle={t("emptyTitle")}
                emptyDescription={t("emptyDescription")}
                onRowClick={(r) => featuresQ.data && setOpen({ plan: r, product: r.product })}
                tableMinWidth={760}
                compact
              />
            </Section>
          ))}
        </div>
      )}
      {open && (
        <PlanDrawer key={open.plan?.plan_key ?? "new"} plan={open.plan} product={open.product} catalogue={catalogue}
          onClose={() => setOpen(null)} />
      )}
    </>
  );
}
