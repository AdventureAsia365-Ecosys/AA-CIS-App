"use client";
// app/admin/tenants/TenantActivity.tsx — AA-1140 (S289): tenant activity of the last 7 days and the quota outliers,
// moved here from the old Overview when /admin/overview became the Ecosystem status page (S285 advisory: tenant
// quotas are not ops status). Same data as before (GET /api/admin/overview, the query Data coverage shares, ops.read):
// shown only to a role that may read it, so a Tenants viewer without ops.read never fires a refused call.

import { DataTable, K, KpiTile, Section, useAdminIdentity, useT } from "../../_kit";
import { can } from "@/lib/rbac";
import { OVERVIEW } from "../overview/messages";
import { hintColumn } from "../overview/_components/opsUi";
import { useSources } from "../overview/_components/sources";
import { useOverviewContent, type OverviewContent } from "../data-coverage/ContentPipeline";

type Outlier = OverviewContent["tenants"]["quota_outliers"][number];

function pctColor(v: number): string {
  return v >= 100 ? K.danger : K.amber;
}

function Activity() {
  const t = useT(OVERVIEW);
  const { TENANT_SRC } = useSources();
  const { data } = useOverviewContent();
  if (!data) return null;
  const tn = data.tenants;
  return (
    <Section title={t("tenants")} testId="tenants-activity" style={{ marginBottom: 18 }}
      bodyStyle={{ display: "grid", gap: 12 }}>
      <div style={{ display: "grid", gap: 12, gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))" }}>
        <KpiTile size="sm" label={t("activeTenants")} value={tn.active_tenants} info={TENANT_SRC.active} />
        <KpiTile size="sm" label={t("rewrites7d")} value={tn.rewrites_7d} info={TENANT_SRC.rewrites} />
        <KpiTile size="sm" label={t("pieces7d")} value={tn.pieces_7d} info={TENANT_SRC.pieces} />
        <KpiTile size="sm" label={t("published7d")} value={tn.published_pieces_7d} info={TENANT_SRC.published} />
      </div>
      {tn.quota_outliers.length > 0 && (
        <DataTable<Outlier> tableId="tenants-quota-outliers" data={tn.quota_outliers} getRowId={(q) => q.tenant_name}
          searchable={false} paginate={false} compact
          columns={[
            { id: t("quotaOutlier"), header: t("quotaOutlier"), enableSorting: false,
              cell: ({ row }) => <span style={{ fontWeight: 600, color: K.ink }}>{row.original.tenant_name}</span> },
            { id: t("plan"), header: t("plan"), enableSorting: false, cell: ({ row }) => row.original.plan_tier },
            { ...hintColumn(t("toursPct"), TENANT_SRC.quotaTours),
              cell: ({ row }) => <span style={{ color: pctColor(row.original.quota_tours_pct) }}>
                {row.original.quota_tours_pct}%</span> },
            { ...hintColumn(t("apiPct"), TENANT_SRC.quotaCalls),
              cell: ({ row }) => <span style={{ color: pctColor(row.original.quota_calls_pct) }}>
                {row.original.quota_calls_pct}%</span> },
          ]} />
      )}
    </Section>
  );
}

/** Tenant activity and quota outliers, for a role with ops.read (the read behind it is admin-only). */
export function TenantActivity() {
  const { role } = useAdminIdentity();
  return can(role, "ops.read") ? <Activity /> : null;
}
