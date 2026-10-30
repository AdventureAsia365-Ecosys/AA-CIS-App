"use client";
// app/admin/settings/CatalogTab.tsx
// AA-686 — the Catalog sub-tab of LLM Models: a kit DataTable of every model in
// shared.llm_model_catalog (label/key, vendor, provider, accounts, price in/out, price_source,
// enabled, blocked_reason). Each row's price + source + enabled are editable inline via
// PATCH /admin/llm-catalog/{model_key}. Enabling a model with no price is disabled in the UI; the
// 422 the API returns on that condition is still surfaced inline if it ever comes back.
// AA-994: text in the UI language (models.messages.ts); catalog values stay as stored.

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, X } from "lucide-react";
import {
  K, mono, sans,
  Badge,
  Button,
  type ColumnDef,
  DataTable,
  apiGet,
  apiSend,
  useFormat,
  useT,
  useToast,
} from "../../_kit";
import { type CatalogRow } from "./llmShared";
import { MODELS } from "./models.messages";
import { useAccountText, useAwsAccounts } from "../_components/useAwsAccounts";
import { accountName } from "@/lib/awsAccounts";

interface CatalogResp {
  models: CatalogRow[];
}
interface CatalogPatchBody {
  price_in_per_mtok?: number;
  price_out_per_mtok?: number;
  price_source?: string;
  enabled?: boolean;
}

const num = (v: number | null | undefined) => (v == null ? "—" : `$${v.toFixed(2)}`);
const accountsOf = (r: CatalogRow) => Object.keys(r.bedrock_profile_ids || {});

function EditRow({ row, onDone }: { row: CatalogRow; onDone: () => void }) {
  const t = useT(MODELS);
  const toast = useToast();
  const qc = useQueryClient();
  const [priceIn, setPriceIn] = useState(row.price_in_per_mtok ?? "");
  const [priceOut, setPriceOut] = useState(row.price_out_per_mtok ?? "");
  const [source, setSource] = useState(row.price_source ?? "");
  const [error, setError] = useState("");

  const mut = useMutation({
    mutationFn: (body: CatalogPatchBody) =>
      apiSend<CatalogRow>(`/api/admin/llm-catalog/${row.model_key}`, {
        method: "PATCH",
        body,
        admin: true,
      }),
    onSuccess: () => {
      toast.success(t("saved", row.model_key));
      qc.invalidateQueries({ queryKey: ["llm-catalog"] });
      onDone();
    },
    onError: (e) => setError(e instanceof Error ? e.message : t("saveFailed")),
  });

  function save() {
    const body: CatalogPatchBody = {};
    if (priceIn !== "" && Number(priceIn) !== row.price_in_per_mtok) body.price_in_per_mtok = Number(priceIn);
    if (priceOut !== "" && Number(priceOut) !== row.price_out_per_mtok) body.price_out_per_mtok = Number(priceOut);
    if (source !== (row.price_source ?? "")) body.price_source = source;
    if (Object.keys(body).length === 0) {
      onDone();
      return;
    }
    setError("");
    mut.mutate(body);
  }

  const inp: React.CSSProperties = {
    width: 90, padding: "5px 8px", borderRadius: 7, border: `1px solid ${K.line}`,
    fontSize: 12.5, fontFamily: mono, color: K.body, background: K.card,
  };
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
        <label style={{ fontSize: 11, color: K.muted }}>{t("inMtok")}
          <input type="number" min={0} step="0.01" value={priceIn}
                 onChange={(e) => setPriceIn(e.target.value)} disabled={mut.isPending} style={{ ...inp, marginLeft: 4 }} />
        </label>
        <label style={{ fontSize: 11, color: K.muted }}>{t("outMtok")}
          <input type="number" min={0} step="0.01" value={priceOut}
                 onChange={(e) => setPriceOut(e.target.value)} disabled={mut.isPending} style={{ ...inp, marginLeft: 4 }} />
        </label>
        <input placeholder={t("priceSource")} value={source} onChange={(e) => setSource(e.target.value)}
               disabled={mut.isPending}
               style={{ width: 160, padding: "5px 8px", borderRadius: 7, border: `1px solid ${K.line}`,
                 fontSize: 12.5, fontFamily: sans, color: K.body, background: K.card }} />
        <Button size="sm" variant="primary" disabled={mut.isPending} onClick={save}>
          {mut.isPending ? t("saving") : t("save")}
        </Button>
        <Button size="sm" variant="ghost" disabled={mut.isPending} onClick={onDone}>{t("cancel")}</Button>
      </div>
      {error && <span data-testid={`catalog-error-${row.model_key}`} style={{ fontSize: 11.5, color: K.danger }}>{error}</span>}
    </div>
  );
}

export function CatalogTab() {
  const t = useT(MODELS);
  const { formatDateTime } = useFormat();
  const toast = useToast();
  const qc = useQueryClient();
  const [editing, setEditing] = useState<string | null>(null);
  const awsAccounts = useAwsAccounts();
  const accountText = useAccountText();

  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["llm-catalog"],
    queryFn: () => apiGet<CatalogResp>("/api/admin/llm-catalog", { admin: true }),
  });

  const toggleMut = useMutation({
    mutationFn: ({ model_key, enabled }: { model_key: string; enabled: boolean }) =>
      apiSend<CatalogRow>(`/api/admin/llm-catalog/${model_key}`, {
        method: "PATCH",
        body: { enabled },
        admin: true,
      }),
    onSuccess: (_r, v) => {
      toast.success(t("toggled", v.model_key, v.enabled));
      qc.invalidateQueries({ queryKey: ["llm-catalog"] });
    },
    // The 422 (enabling without a price) is still shown even though the UI disables the control.
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
          <div>
            <div style={{ fontWeight: 600, color: K.ink }}>{r.label}</div>
            <div style={{ fontFamily: mono, fontSize: 11.5, color: K.muted2 }}>{r.model_key}</div>
          </div>
        );
      },
    },
    { accessorKey: "vendor", header: t("colVendor"), cell: (c) => <span style={{ color: K.body }}>{c.row.original.vendor}</span> },
    { accessorKey: "provider", header: t("colProvider"), cell: (c) => <span style={{ color: K.body }}>{c.row.original.provider}</span> },
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
      cell: (c) => <span style={{ fontFamily: mono }}>{num(c.row.original.price_in_per_mtok)}</span>,
    },
    {
      accessorKey: "price_out_per_mtok",
      header: t("colOut"),
      cell: (c) => <span style={{ fontFamily: mono }}>{num(c.row.original.price_out_per_mtok)}</span>,
    },
    {
      accessorKey: "price_source",
      header: t("colSource"),
      cell: (c) => <span style={{ color: K.muted, fontSize: 12 }}>{c.row.original.price_source ?? "—"}</span>,
    },
    {
      accessorKey: "enabled",
      header: t("colEnabled"),
      cell: (c) => {
        const r = c.row.original;
        const hasPrice = r.price_in_per_mtok != null && r.price_out_per_mtok != null;
        // enabling without a price is disabled in the UI (the API 422s on it too).
        const canToggle = r.enabled || hasPrice;
        return (
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <button
              role="switch"
              aria-checked={r.enabled}
              aria-label={t("toggle", r.model_key)}
              data-testid={`catalog-enabled-${r.model_key}`}
              disabled={!canToggle || toggleMut.isPending}
              onClick={() => toggleMut.mutate({ model_key: r.model_key, enabled: !r.enabled })}
              style={{
                display: "inline-flex", alignItems: "center", gap: 5, padding: "3px 9px", borderRadius: 999,
                border: `1px solid ${r.enabled ? K.accentBorder : K.line}`,
                background: r.enabled ? K.accentTint : K.card, color: r.enabled ? K.accentDeep : K.muted,
                fontSize: 11, fontWeight: 600, cursor: canToggle ? "pointer" : "not-allowed",
                fontFamily: sans, opacity: canToggle ? 1 : 0.5,
              }}
            >
              {r.enabled ? <Check size={11} /> : <X size={11} />}
              {r.enabled ? t("onTitle") : t("offTitle")}
            </button>
            {!r.enabled && !hasPrice && (
              <span title={t("needsPriceTitle")} style={{ fontSize: 10.5, color: K.muted2 }}>
                {t("needsPrice")}
              </span>
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
        return r.blocked_reason
          ? <Badge tone="warning" dot={false}>{r.blocked_reason}</Badge>
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
    {
      id: "actions",
      header: "",
      enableSorting: false,
      enableHiding: false,
      cell: (c) => (
        <div onClick={(e) => e.stopPropagation()}>
          <Button size="sm" variant="secondary" onClick={() => setEditing(c.row.original.model_key)}>
            {t("editPrice")}
          </Button>
        </div>
      ),
    },
  ], [toggleMut, awsAccounts, accountText, t, formatDateTime]);

  const editingRow = models.find((m) => m.model_key === editing) ?? null;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <p style={{ fontSize: 12, color: K.muted2, margin: 0 }}>
        {t("catalogIntro")}
      </p>

      {editingRow && (
        <div style={{
          padding: "12px 14px", borderRadius: 10, border: `1px solid ${K.accentBorder}`,
          background: K.accentTint,
        }}>
          <div style={{ fontSize: 12.5, fontWeight: 600, color: K.ink, marginBottom: 8 }}>
            {t("editPriceFor")} <span style={{ fontFamily: mono }}>{editingRow.model_key}</span>
          </div>
          <EditRow row={editingRow} onDone={() => setEditing(null)} />
        </div>
      )}

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
        tableMinWidth={1040}
        emptyTitle={t("noModels")}
      />
    </div>
  );
}
