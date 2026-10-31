"use client";
// app/admin/ai-gateway/CatalogTable.tsx
// AA-686 — every model in shared.llm_model_catalog (label/key, vendor, provider, accounts, price in/out,
// price_source, enabled, blocked_reason) as a kit DataTable. AA-804 UI part 2 / AA-1017 moved it from Settings ›
// LLM Models › Catalog to AI providers › Models & trials and made the prices view-only: a price changes through a
// reviewed configuration change (migration), not from the admin. The only edit left is the on/off switch
// (PATCH /admin/llm-catalog/{model_key} {enabled}); switching on a model with no price is disabled here and refused
// by the API (422, shown in a toast).
// AA-994: text in the UI language (models.messages.ts); catalog values stay as stored.
// C4 live findings F3 / F4: vendor and provider share one column and the model name keeps a minimum width (it wrapped
// over 3–4 lines); the blocked reason is a "Blocked" badge with a plain sentence under it (no session tag, no capitals)
// and the price source leaves out code file and migration names (lib/aiGateway blockedReasonText / priceSourceText).

import { useMemo } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Badge, DataTable, K, Switch, apiGet, apiSend, mono, useFormat, useT, useToast, type ColumnDef,
} from "../../_kit";
import { blockedReasonText, priceSourceText } from "@/lib/aiGateway";
import { accountName } from "@/lib/awsAccounts";
import { useAccountText, useAwsAccounts } from "../_components/useAwsAccounts";
import { type CatalogRow } from "./llmShared";
import { MODELS } from "./models.messages";

interface CatalogResp {
  models: CatalogRow[];
}

export const LLM_CATALOG_KEY = ["llm-catalog"] as const;

const num = (v: number | null | undefined) => (v == null ? "—" : `$${v.toFixed(2)}`);
const accountsOf = (r: CatalogRow) => Object.keys(r.bedrock_profile_ids || {});

export function CatalogTable() {
  const t = useT(MODELS);
  const { formatDateTime } = useFormat();
  const toast = useToast();
  const qc = useQueryClient();
  const awsAccounts = useAwsAccounts();
  const accountText = useAccountText();

  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: LLM_CATALOG_KEY,
    queryFn: () => apiGet<CatalogResp>("/api/admin/llm-catalog", { admin: true }),
  });

  const toggleMut = useMutation({
    mutationFn: ({ model_key, enabled }: { model_key: string; enabled: boolean }) =>
      apiSend<CatalogRow>(`/api/admin/llm-catalog/${model_key}`, { method: "PATCH", body: { enabled }, admin: true }),
    onSuccess: (_r, v) => {
      toast.success(t("toggled", v.model_key, v.enabled));
      qc.invalidateQueries({ queryKey: LLM_CATALOG_KEY });
    },
    // The 422 (switching on without a price) is still shown even though the UI disables the control.
    onError: (e, v) => toast.error(`${v.model_key}: ${e instanceof Error ? e.message : t("updateFailed")}`),
  });

  const models = useMemo(() => data?.models ?? [], [data]);

  const columns = useMemo<ColumnDef<CatalogRow, unknown>[]>(() => [
    {
      accessorKey: "label",
      header: t("colModel"),
      cell: (c) => {
        const r = c.row.original;
        return (
          <div style={{ minWidth: 170 }}>
            <div style={{ fontWeight: 600, color: K.ink }}>{r.label}</div>
            <div style={{ fontFamily: mono, fontSize: 11.5, color: K.muted2 }}>{r.model_key}</div>
          </div>
        );
      },
    },
    { id: "vendor", accessorFn: (r) => `${r.vendor} ${r.provider}`, header: t("colVendorProvider"),
      cell: (c) => (
        <div style={{ display: "grid", gap: 2 }}>
          <span style={{ color: K.body }}>{c.row.original.vendor}</span>
          <span style={{ fontSize: 12, color: K.muted }}>{c.row.original.provider}</span>
        </div>
      ) },
    {
      id: "accounts",
      header: t("colAccounts"),
      enableSorting: false,
      cell: (c) => {
        const accts = accountsOf(c.row.original);
        return (
          <span style={{ fontFamily: mono, fontSize: 12, color: K.muted }}>
            {accts.length ? accts.map((a) => accountName(a, awsAccounts, accountText)).join(", ") : "—"}
          </span>
        );
      },
    },
    {
      accessorKey: "price_in_per_mtok",
      header: t("colIn"),
      meta: { align: "right" },
      cell: (c) => <span style={{ fontFamily: mono }}>{num(c.row.original.price_in_per_mtok)}</span>,
    },
    {
      accessorKey: "price_out_per_mtok",
      header: t("colOut"),
      meta: { align: "right" },
      cell: (c) => <span style={{ fontFamily: mono }}>{num(c.row.original.price_out_per_mtok)}</span>,
    },
    {
      accessorKey: "price_source",
      header: t("colSource"),
      cell: (c) => (
        <span style={{ color: K.muted, fontSize: 12, display: "block", minWidth: 160, maxWidth: 260 }}>
          {priceSourceText(c.row.original.price_source, t("builtInPrices")) ?? "—"}
        </span>
      ),
    },
    {
      accessorKey: "enabled",
      header: t("colEnabled"),
      cell: (c) => {
        const r = c.row.original;
        const hasPrice = r.price_in_per_mtok != null && r.price_out_per_mtok != null;
        // switching on without a price is disabled in the UI (the API 422s on it too).
        const canToggle = r.enabled || hasPrice;
        return (
          <div style={{ display: "flex", alignItems: "center", gap: 8 }} onClick={(e) => e.stopPropagation()}>
            <Switch
              checked={r.enabled}
              label={t("toggle", r.model_key)}
              testId={`catalog-enabled-${r.model_key}`}
              disabled={!canToggle || toggleMut.isPending}
              onChange={(next) => toggleMut.mutate({ model_key: r.model_key, enabled: next })}
            />
            <span style={{ fontSize: 12, color: r.enabled ? K.ink : K.muted }}>
              {r.enabled ? t("onTitle") : t("offTitle")}
            </span>
            {!r.enabled && !hasPrice && (
              <span title={t("needsPriceTitle")} style={{ fontSize: 11, color: K.muted2 }}>{t("needsPrice")}</span>
            )}
          </div>
        );
      },
    },
    {
      id: "blocked_reason",
      header: t("colBlocked"),
      enableSorting: false,
      cell: (c) => {
        const r = c.row.original;
        const why = blockedReasonText(r.blocked_reason);
        return why
          ? (
            <div style={{ display: "grid", gap: 4, justifyItems: "start", minWidth: 150, maxWidth: 240 }}>
              <Badge tone="warning" dot={false} style={{ whiteSpace: "nowrap" }}>{t("blockedBadge")}</Badge>
              <span style={{ fontSize: 12, color: K.muted, lineHeight: 1.4 }}>{why}</span>
            </div>
          )
          : <span style={{ color: K.muted2 }}>—</span>;
      },
    },
    {
      id: "updated",
      header: t("colUpdated"),
      cell: (c) => (
        <span style={{ fontSize: 11.5, color: K.muted2, whiteSpace: "nowrap" }}>
          {formatDateTime(c.row.original.updated_at)}
        </span>
      ),
    },
  ], [toggleMut, awsAccounts, accountText, t, formatDateTime]);

  return (
    <DataTable<CatalogRow>
      data={models}
      columns={columns}
      tableId="admin-llm-catalog"
      getRowId={(r) => r.model_key}
      loading={isLoading}
      error={isError ? (error instanceof Error ? error.message : t("loadCatalogError")) : null}
      onRetry={() => refetch()}
      searchable
      searchPlaceholder={t("searchModels")}
      enableCsv
      csvFilename="llm-catalog"
      pageSize={25}
      pageSizeOptions={[25, 50, 100]}
      tableMinWidth={1000}
      emptyTitle={t("noModels")}
    />
  );
}
