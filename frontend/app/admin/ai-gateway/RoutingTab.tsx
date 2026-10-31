"use client";
// app/admin/ai-gateway/RoutingTab.tsx — AA-804 UI part 2 / AA-1017: the Routing tab of AI providers (was the Stages
// sub-tab of Settings › LLM Models, AA-686).
//
// One row per stage, grouped like the pipeline (llmShared.ts STAGE_GROUPS): the primary model (PATCH
// /api/admin/llm-config/{stage}), the route the gateway really runs (chain + shadow), and the fallback / shadow editor
// (StageRouteEditor, PATCH /api/admin/llm-config/{stage}/route). The LLM availability card sits on top (AA-900) and the
// Jev questions per stage at the bottom (read-only, from the Jev Decisions summary).
// The per-stage account select (AA-1017 D10) shows only while a stage still routes to another account than the LLM
// account (lib/aiGateway accountRouteMatters); hidden, the save sends the row's own account route back unchanged.
// GET /api/admin/llm-config keeps its path and access (the S1 rewrite page reads it too). Kit only.
// C4 (S262 wave 4, AA-1018 UI): each stage carries the failure-domain rule's answer (GET /api/admin/llm-availability
// `invariant`, report only): "Fallback in another failure domain" or "No fallback in another failure domain" with why.
// An API without the report shows no badge.

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle } from "lucide-react";
import {
  Badge, Button, ConfirmModal, DataTable, ErrorState, K, LoadingScreen, Section, Select, Spinner, apiGet, apiSend,
  mono, useFormat, useLocalized, useT, useToast, type ColumnDef,
} from "../../_kit";
import {
  accountRouteMatters, actorLabel, invariantByStage, type Invariant, type InvariantStage,
} from "@/lib/aiGateway";
import { accountName } from "@/lib/awsAccounts";
import { useAccountText, useAwsAccounts } from "../_components/useAwsAccounts";
import {
  HIDDEN_STAGES, ROLE_TONE, STAGE_GROUPS, STAGE_LABELS, type StageConfigRow, type StageRoute, withVia,
} from "./llmShared";
import { STAGE_GROUP_LABELS_VI, STAGE_LABELS_VI } from "./llmShared.vi";
import { LlmAvailabilityCard } from "./LlmAvailabilityCard";
import { MODELS } from "./models.messages";
import { StageRouteEditor } from "./StageRouteEditor";
import { useDomainText } from "./useDomainText";

const STAGE_GROUP_LABELS: Record<string, string> = Object.fromEntries(STAGE_GROUPS.map((g) => [g.key, g.label]));

export const LLM_CONFIG_KEY = ["llm-config"] as const;

interface LlmConfigResp {
  stages: StageConfigRow[];
}

const small = { fontSize: 12, color: K.muted2, lineHeight: 1.5 } as const;

// ── Route line (read-only summary of chain + shadow) ───────────────────────────────────────────────────────────────
function RouteLine({ route }: { route?: StageRoute }) {
  const t = useT(MODELS);
  if (!route || route.chain.length === 0) return null;
  const chip = (label: string, via: string | null | undefined, key: string, tone: "main" | "fb" | "shadow") => (
    <Badge key={key} tone={tone === "main" ? "accent" : "neutral"} dot={false}>
      <span style={{ fontWeight: tone === "main" ? 600 : 400, opacity: tone === "shadow" ? 0.75 : 1 }}>
        {label}{via ? ` · ${via}` : ""}
      </span>
    </Badge>
  );
  return (
    <div data-testid="llm-route-line" style={{
      width: "100%", display: "flex", flexWrap: "wrap", alignItems: "center", gap: 6, fontSize: 11.5, color: K.muted2,
    }}>
      <span>{t("route")}</span>
      {route.chain.map((i, n) => (
        <span key={`${i.model_id}-${n}`} style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
          {n > 0 && <span aria-hidden>→</span>}
          {chip(i.label, i.via, i.model_id, n === 0 ? "main" : "fb")}
        </span>
      ))}
      {route.shadow && (
        <>
          <span style={{ marginLeft: 8 }}>{t("shadowCompare", route.shadow.sample_pct)}</span>
          {chip(route.shadow.label, route.shadow.via, "shadow", "shadow")}
        </>
      )}
    </div>
  );
}

// ── Failure-domain badge (AA-1018 invariant, report only) ─────────────────────────────────────────────────────────
function DomainBadge({ result }: { result: InvariantStage }) {
  const t = useT(MODELS);
  const domainText = useDomainText();
  const others = result.other_domains.map((d) => domainText(d)).filter(Boolean).join("; ");
  const why = result.ok ? t("domainOkWhy", others)
    : result.primary_domain ? t("domainBreachOne", domainText(result.primary_domain) ?? "") : t("domainBreachNone");
  return (
    <span data-testid={`llm-domain-${result.stage}`} title={why}
      style={{ display: "inline-flex", alignItems: "center", gap: 6, minWidth: 0, flexWrap: "wrap" }}>
      <Badge tone={result.ok ? "success" : "warning"} dot={false} style={{ whiteSpace: "nowrap" }}>
        {result.ok ? t("domainOk") : t("domainBreach")}
      </Badge>
    </span>
  );
}

function DomainLine({ result }: { result: InvariantStage }) {
  const t = useT(MODELS);
  const domainText = useDomainText();
  if (result.ok) return null;
  return (
    <div style={{ fontSize: 11.5, color: K.amber, lineHeight: 1.5 }}>
      {result.primary_domain ? t("domainBreachOne", domainText(result.primary_domain) ?? "") : t("domainBreachNone")}
    </div>
  );
}

// ── Primary-model row (PATCH /llm-config/{stage}) + its route editor ───────────────────────────────────────────────
function ModelRow({ row, showAccount, domain }: {
  row: StageConfigRow; showAccount: boolean; domain?: InvariantStage;
}) {
  const t = useT(MODELS);
  const { formatDateTime } = useFormat();
  const stageLabels = useLocalized({ en: STAGE_LABELS, vi: STAGE_LABELS_VI });
  const toast = useToast();
  const qc = useQueryClient();
  const [modelId, setModelId] = useState(row.model_id);
  const [accountRoute, setAccountRoute] = useState(row.account_route ?? "");
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState("");
  const awsAccounts = useAwsAccounts();
  const accountText = useAccountText();

  const accountSelect = showAccount && row.account_route_options.length > 0;
  const dirty = modelId !== row.model_id || (accountSelect && accountRoute !== (row.account_route ?? ""));
  const modelLabel = (id: string) => {
    const o = row.options.find((x) => x.model_id === id);
    return o ? withVia(o.label, o.via) : id;
  };
  // A route outside the options (a non-Claude stage has none) still shows the account ID, never the key (AA-998).
  const acctLabel = (v: string) =>
    row.account_route_options.find((o) => o.value === v)?.label ?? accountName(v, awsAccounts, accountText);

  const mut = useMutation({
    mutationFn: (body: { model_id: string; account_route: string | null }) =>
      apiSend<StageConfigRow>(`/api/admin/llm-config/${row.stage}`, { method: "PATCH", body, admin: true }),
    onSuccess: () => {
      toast.success(t("modelSaved", row.stage));
      setError("");
      qc.invalidateQueries({ queryKey: LLM_CONFIG_KEY });
    },
    onError: (e) => {
      setError(e instanceof Error ? e.message : t("saveFailed"));
      setModelId(row.model_id);
      setAccountRoute(row.account_route ?? "");
    },
  });

  function confirmSave() {
    setConfirming(false);
    setError("");
    // Hidden select: send the row's own route back, so a model change never moves the stage to another account.
    const route = accountSelect ? accountRoute || null : row.account_route;
    mut.mutate({ model_id: modelId, account_route: route });
  }

  const saving = mut.isPending;
  const selected = row.options.find((o) => o.model_id === modelId);
  const nextRoute = accountSelect ? accountRoute : row.account_route ?? "";

  return (
    <div data-testid={`llm-model-row-${row.stage}`} style={{
      display: "flex", flexDirection: "column", gap: 8, padding: "12px 0", borderBottom: `1px solid ${K.line2}`,
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <div style={{ minWidth: 0, flex: "1 1 200px" }}>
          <div style={{ fontFamily: mono, fontSize: 12, color: K.ink, fontWeight: 600 }}>{row.stage}</div>
          <div style={{ fontSize: 11.5, color: K.muted2 }}>
            {stageLabels[row.stage] ?? STAGE_LABELS[row.stage] ?? row.stage}
          </div>
        </div>
        <Badge tone={ROLE_TONE[row.role] ?? "neutral"} dot={false}>{row.role}</Badge>
        {domain && <DomainBadge result={domain} />}
        <Select size="sm" data-testid={`llm-model-select-${row.stage}`} aria-label={t("primaryFor", row.stage)}
          value={modelId} onChange={(e) => setModelId(e.target.value)} disabled={saving}
          style={{ flex: "1 1 260px", minWidth: 0 }}
          options={row.options.map((o) => ({
            value: o.model_id, disabled: !o.available,
            label: `${withVia(o.label, o.via)}${!o.available ? t("notAvailable") : ""}`,
          }))} />
        {!selected?.available && selected?.reason ? (
          <span title={selected.reason} style={{ display: "inline-flex", color: K.amber }}>
            <AlertTriangle size={13} />
          </span>
        ) : null}
        {accountSelect && (
          <Select size="sm" data-testid={`llm-account-select-${row.stage}`} aria-label={t("accountFor", row.stage)}
            value={accountRoute} onChange={(e) => setAccountRoute(e.target.value)} disabled={saving}
            style={{ flex: "0 1 180px", minWidth: 0 }}
            options={row.account_route_options.map((o) => ({ value: o.value, label: o.label }))} />
        )}
        <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          {error && <span style={{ fontSize: 11.5, color: K.danger }}>{error}</span>}
          <span style={{ fontSize: 11, color: K.muted2 }}>
            {row.updated_at ? formatDateTime(row.updated_at) : "—"} · {actorLabel(row, t("staffMember")) ?? "—"}
          </span>
          <Button variant="secondary" size="sm" disabled={!dirty || saving} onClick={() => setConfirming(true)}
            testId={`llm-model-save-${row.stage}`}>
            {saving ? <Spinner size={12} /> : null}
            {saving ? t("saving") : t("save")}
          </Button>
        </div>
      </div>

      <RouteLine route={row.route} />
      {domain && <DomainLine result={domain} />}

      {/* AA-686 — fallback chain + shadow A/B editor */}
      <StageRouteEditor row={row} />

      <ConfirmModal
        open={confirming}
        onClose={() => setConfirming(false)}
        onConfirm={confirmSave}
        title={t("changeModelTitle", row.stage)}
        confirmLabel={t("confirmChange")}
        body={
          <div>
            <p style={{ fontSize: 12.5, color: K.muted, marginTop: 0, marginBottom: 12 }}>{t("changeModelBody")}</p>
            <div style={{
              background: K.line2, borderRadius: 8, padding: "10px 12px", fontSize: 12.5, fontFamily: mono,
              color: K.ink2,
            }}>
              {modelLabel(row.model_id)}{row.account_route ? ` (${acctLabel(row.account_route)})` : ""}
              {" → "}
              {modelLabel(modelId)}{nextRoute ? ` (${acctLabel(nextRoute)})` : ""}
            </div>
          </div>
        }
      />
    </div>
  );
}

// ── Jev questions per stage (read-only) ────────────────────────────────────────────────────────────────────────────
interface JevQuestionRow {
  question_key: string; stage: string; kind: string; mode: "off" | "shadow" | "enforce";
  accept_floor: number | null; reject_ceiling: number | null; calibration_ref: string | null;
  verdicts: number; acted: number; cost_usd: number;
}
interface JevSummaryResp { questions?: JevQuestionRow[] }

const MODE_TONE = { off: "neutral", shadow: "info", enforce: "accent" } as const;

function JevStagesCard() {
  const t = useT(MODELS);
  const q = useQuery({
    queryKey: ["decisions-summary", 7],
    queryFn: () => apiGet<JevSummaryResp>("/api/admin/decisions/summary?days=7", { admin: true }),
  });
  const rows = useMemo(() => q.data?.questions ?? [], [q.data]);
  const columns = useMemo<ColumnDef<JevQuestionRow, unknown>[]>(() => [
    { accessorKey: "stage", header: t("colStage"),
      cell: (c) => <span style={{ fontFamily: mono }}>{c.row.original.stage}</span> },
    { accessorKey: "question_key", header: t("colQuestion"),
      cell: (c) => <span style={{ fontFamily: mono }}>{c.row.original.question_key}</span> },
    { accessorKey: "mode", header: t("colMode"),
      cell: (c) => <Badge tone={MODE_TONE[c.row.original.mode] ?? "neutral"} dot={false}>{c.row.original.mode}</Badge> },
    { id: "bands", header: t("colBands"), enableSorting: false,
      cell: (c) => (
        <span style={{ fontFamily: mono }}>{c.row.original.accept_floor ?? "—"} / {c.row.original.reject_ceiling ?? "—"}</span>
      ) },
    { id: "calibration", header: t("colCalibration"), accessorFn: (r) => (r.calibration_ref ? 1 : 0),
      cell: (c) => (
        <span style={{ color: K.muted }}>{c.row.original.calibration_ref ? t("calibrated") : t("notCalibrated")}</span>
      ) },
    { id: "verdicts", header: t("colVerdicts"), accessorKey: "verdicts", meta: { align: "right" },
      cell: (c) => <span style={{ fontFamily: mono }}>{t("verdictsActed", c.row.original.verdicts, c.row.original.acted)}</span> },
    { id: "cost", header: t("colCost7d"), accessorKey: "cost_usd", meta: { align: "right" },
      cell: (c) => <span style={{ fontFamily: mono }}>${c.row.original.cost_usd.toFixed(5)}</span> },
  ], [t]);
  return (
    <Section title={t("jevTitle")}
      description={<>{t("jevIntroA")} <b>{t("jevIntroB")}</b> {t("jevIntroC")}{" "}
        <a href="/admin/decisions" style={{ color: K.accent }}>{t("jevDecisions")}</a>.</>}
      padded={false}>
      <DataTable<JevQuestionRow>
        tableId="ai-gateway-jev-stages" data={rows} columns={columns} getRowId={(r) => r.question_key}
        loading={q.isLoading} error={q.isError ? t("loadJevStagesError") : null} onRetry={() => q.refetch()}
        searchable={false} paginate={false} stickyHeader={false} tableMinWidth={760}
        emptyTitle={t("noJevQuestions")}
      />
    </Section>
  );
}

// ── the tab ────────────────────────────────────────────────────────────────────────────────────────────────────────
export default function RoutingTab() {
  const t = useT(MODELS);
  const groupLabels = useLocalized({ en: STAGE_GROUP_LABELS, vi: STAGE_GROUP_LABELS_VI });
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: LLM_CONFIG_KEY,
    queryFn: () => apiGet<LlmConfigResp>("/api/admin/llm-config", { admin: true }),
  });
  const rows = useMemo(() => data?.stages ?? [], [data]);
  const showAccount = useMemo(() => accountRouteMatters(rows), [rows]);
  // Same cache as the availability card above (one read for both).
  const avail = useQuery({
    queryKey: ["llm-availability", 24],
    queryFn: () => apiGet<{ invariant?: Invariant }>("/api/admin/llm-availability?hours=24", { admin: true }),
    refetchInterval: 60_000,
  });
  const domains = useMemo(() => invariantByStage(avail.data?.invariant), [avail.data]);

  if (isLoading && !data) return <LoadingScreen message={t("loadingConfig")} />;
  if (isError || !data) {
    return (
      <ErrorState message={`${t("loadConfigError")}${error instanceof Error ? `: ${error.message}` : ""}`}
        onRetry={() => refetch()} />
    );
  }

  const byStage = Object.fromEntries(rows.map((r) => [r.stage, r]));
  return (
    <div data-testid="ai-gateway-routing-ready"
      style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 16 }}>
      <LlmAvailabilityCard />
      <p style={{ ...small, margin: 0 }}>
        {t("stagesIntroA")} <b>{t("stagesIntroB")}</b>{t("stagesIntroC")} <b>{t("stagesIntroD")}</b>{" "}
        {t("stagesIntroE")}
        {!showAccount && <> {t("accountHidden")}</>}
      </p>
      {STAGE_GROUPS.map((group) => {
        const stages = group.stages.filter((s) => !HIDDEN_STAGES.has(s) && byStage[s]);
        if (stages.length === 0) return null;
        return (
          <Section key={group.key} title={groupLabels[group.key] ?? group.label}>
            <div style={{ marginTop: -12 }}>
              {stages.map((stage) => (
                <ModelRow key={stage} row={byStage[stage]} showAccount={showAccount} domain={domains[stage]} />
              ))}
            </div>
          </Section>
        );
      })}
      <JevStagesCard />
    </div>
  );
}
