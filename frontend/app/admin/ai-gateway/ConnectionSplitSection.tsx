"use client";
// app/admin/ai-gateway/ConnectionSplitSection.tsx — AA-1113 (S284 tech lead redesign): AI providers › Budgets,
// "Connection budget split". Per LLM connection: the provider budget (CIS setting
// cis.spend.connection_provider_budget_usd), the CIS share (the connection's monthly budget), the CRM share (the CRM
// setting crm.ai.connection_monthly_cap_usd, read through the CRM settings relay), what is left, and a red flag when
// the shares add up to more than the provider budget. One read, GET /api/admin/ai-gateway/connection-split
// (lib/connectionSplit.ts); its own query, so a CRM that does not answer never blanks the rest of the tab. Edits stay
// where each value lives: the provider budget and the CRM share on Platform › Settings, the CIS share on the
// connection (Providers › Connections). Kit only (Section, DataTable, Badge, ErrorState).

import { useMemo } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { Badge, DataTable, ErrorState, K, Section, apiGet, mono, useT, type ColumnDef } from "../../_kit";
import { SPLIT as M, SPLIT_STATE } from "./connectionSplit.messages";
import { usd } from "@/lib/aiGateway";
import {
  CONNECTION_SPLIT_API, missingShareApps, splitOrder, splitTone, type SplitResp, type SplitRow, type SplitState,
} from "@/lib/connectionSplit";

export const CONNECTION_SPLIT_KEY = ["ai-gateway", "connection-split"] as const;

const small = { fontSize: 12, color: K.muted, lineHeight: 1.45 } as const;
const PROVIDER_HREF = "/admin/platform/settings?app=cis&area=spend&key=cis.spend.connection_provider_budget_usd";
const CRM_HREF = "/admin/platform/settings?app=crm&area=ai&key=crm.ai.connection_monthly_cap_usd";

function Amount({ value, empty }: { value: number | null; empty: string }) {
  return <span style={{ fontFamily: mono }}>{value == null ? empty : usd(value, 2)}</span>;
}

// The notices above the table (CRM unreadable, a setting unreadable, stray keys); its own component keeps the section's
// complexity under the code-health limit.
function SplitNotices({ data }: { data: SplitResp }) {
  const t = useT(M);
  if (!data.crm_error && data.errors.length === 0 && data.unknown_keys.length === 0) return null;
  return (
    <div role="status" style={{ ...small, padding: "8px 16px", display: "grid", gap: 2 }}>
      {data.crm_error && <span style={{ color: K.amber }}>{t("crmError", data.crm_error)}</span>}
      {data.errors.some((e) => e.source === "provider_budget")
        && <span style={{ color: K.danger }}>{t("sourceError")}</span>}
      {data.errors.some((e) => e.source === "cost_policy")
        && <span style={{ color: K.amber }}>{t("policyError")}</span>}
      {data.unknown_keys.length > 0 && <span>{t("stray", data.unknown_keys.join(", "))}</span>}
    </div>
  );
}

export default function ConnectionSplitSection() {
  const t = useT(M);
  const ts = useT(SPLIT_STATE);
  const q = useQuery({
    queryKey: CONNECTION_SPLIT_KEY,
    queryFn: () => apiGet<SplitResp>(CONNECTION_SPLIT_API, { admin: true }),
    refetchInterval: 120_000,
    refetchIntervalInBackground: false,
  });
  const data = q.data;
  const columns = useMemo<ColumnDef<SplitRow, unknown>[]>(() => [
    { id: "connection", header: t("colConnection"), accessorKey: "connection_key",
      cell: (c) => (
        <div style={{ display: "grid", gap: 2, minWidth: 0 }}>
          <span style={{ fontWeight: 600, color: K.ink }}>{c.row.original.label}</span>
          <span style={{ fontFamily: mono, fontSize: 12, color: K.muted2 }}>{c.row.original.connection_key}</span>
        </div>
      ) },
    { id: "provider", header: t("colProvider"), meta: { align: "right" },
      accessorFn: (r) => r.provider_budget_usd ?? -1,
      cell: (c) => <Amount value={c.row.original.provider_budget_usd} empty={t("notSet")} /> },
    { id: "cis", header: t("colCis"), meta: { align: "right" }, accessorFn: (r) => r.cis_share_usd ?? -1,
      cell: (c) => {
        const r = c.row.original;
        return (
          <div style={{ display: "grid", gap: 2, justifyItems: "end" }}>
            <Amount value={r.cis_share_usd} empty={t("noCap")} />
            {(r.alert_only_apps ?? []).includes("cis") && <span style={small}>{t("alertOnly")}</span>}
          </div>
        );
      } },
    { id: "crm", header: t("colCrm"), meta: { align: "right" }, accessorFn: (r) => r.crm_share_usd ?? -1,
      cell: (c) => <Amount value={c.row.original.crm_share_usd}
        empty={c.row.original.crm_known ? t("noCap") : t("unknown")} /> },
    { id: "left", header: t("colLeft"), meta: { align: "right" }, accessorFn: (r) => r.headroom_usd ?? 0,
      cell: (c) => {
        const r = c.row.original;
        if (r.headroom_usd == null) return <span style={small}>—</span>;
        return r.headroom_usd < 0
          ? <span style={{ fontFamily: mono, color: K.danger }}>{t("overBy", usd(-r.headroom_usd, 2))}</span>
          : <span style={{ fontFamily: mono }}>{usd(r.headroom_usd, 2)}</span>;
      } },
    { id: "state", header: t("colState"), accessorKey: "state",
      cell: (c) => {
        const r = c.row.original;
        return (
          <div style={{ display: "grid", gap: 2 }}>
            <Badge tone={splitTone(r.state)} dot={false} style={{ whiteSpace: "nowrap" }}>
              {ts(r.state as SplitState)}
            </Badge>
            {missingShareApps(r).length > 0 && r.state !== "no_provider_budget" && (
              <span style={small}>{t("uncapped", missingShareApps(r).map((a) => a.toUpperCase()).join(", "))}</span>
            )}
            {(r.alert_only_apps ?? []).length > 0 && r.state !== "no_provider_budget" && (
              <span style={small}>{t("alertOnlyApps", r.alert_only_apps.map((a) => a.toUpperCase()).join(", "))}</span>
            )}
          </div>
        );
      } },
  ], [t, ts]);

  if (q.error) {
    return (
      <Section title={t("title")} description={t("help")} testId="ai-gateway-budgets-connection-split">
        <ErrorState message={`${t("loadError")}: ${(q.error as Error).message}`} onRetry={() => q.refetch()} />
      </Section>
    );
  }
  return (
    <Section title={t("title")} description={t("help")} padded={false} testId="ai-gateway-budgets-connection-split"
      actions={(
        <span style={{ display: "flex", gap: 12, flexWrap: "wrap", fontSize: 13 }}>
          <Link href={PROVIDER_HREF}>{t("editProvider")}</Link>
          <Link href={CRM_HREF}>{t("editCrm")}</Link>
        </span>
      )}>
      {data && <SplitNotices data={data} />}
      <DataTable<SplitRow>
        tableId="ai-gateway-budgets-connection-split" data={data ? splitOrder(data.rows) : []} columns={columns}
        getRowId={(r) => r.connection_key} loading={!data} searchable={false} paginate={false} stickyHeader={false}
        tableMinWidth={760} emptyTitle={t("empty")}
      />
    </Section>
  );
}
