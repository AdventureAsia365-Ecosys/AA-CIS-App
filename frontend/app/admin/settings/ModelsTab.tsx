"use client";
// app/admin/settings/ModelsTab.tsx
// AA-686 — the LLM Models tab, split out of settings/page.tsx into its own files. It has three
// sub-tabs:
//   • Stages   — per-stage primary model (PATCH /llm-config/{stage}), plus the fallback-chain +
//                shadow-A/B editor (StageRouteEditor, PATCH /llm-config/{stage}/route).
//   • Catalog  — every catalog model, editable price/source/enabled (CatalogTab).
//   • Shadow A/B — the shadow comparison report (ShadowReportTab).
// AA-900: the Stages sub-tab opens with the LLM availability card (LlmAvailabilityCard).
// Dead stages (HIDDEN_STAGES) are not rendered in the Stages editor.
// Built on the kit (Tabs/Badge/Button/ConfirmModal/DataTable/Toast) + react-query.
// AA-994: text in the UI language (models.messages.ts; stage labels llmShared.ts + llmShared.vi.ts).

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle } from "lucide-react";
import { A, mono, Card, SLabel, Spinner, LoadingScreen } from "../_components/adminUi";
import {
  Button,
  Badge,
  ConfirmModal,
  Tabs,
  ToastProvider,
  apiGet,
  apiSend,
  useFormat,
  useLocalized,
  useT,
  useToast, ScrollX,
} from "../../_kit";
import {
  HIDDEN_STAGES,
  ROLE_TONE,
  STAGE_GROUPS,
  STAGE_LABELS,
  type AccountOption,
  type ModelOption,
  type StageConfigRow,
  type StageRoute,
  withVia,
} from "./llmShared";
import { STAGE_GROUP_LABELS_VI, STAGE_LABELS_VI } from "./llmShared.vi";
import { MODELS } from "./models.messages";
import { StageRouteEditor } from "./StageRouteEditor";
import { LlmAvailabilityCard } from "./LlmAvailabilityCard";
import { useAccountText, useAwsAccounts } from "../_components/useAwsAccounts";
import { accountName } from "@/lib/awsAccounts";

const STAGE_GROUP_LABELS: Record<string, string> = Object.fromEntries(STAGE_GROUPS.map((g) => [g.key, g.label]));
import { CatalogTab } from "./CatalogTab";
import { ShadowReportTab } from "./ShadowReportTab";

interface LlmConfigResp {
  stages: StageConfigRow[];
}

// ── Route line (read-only summary of chain + shadow) ───────────────────────────
function RouteLine({ route }: { route?: StageRoute }) {
  const t = useT(MODELS);
  if (!route || route.chain.length === 0) return null;
  const chip = (label: string, via: string | null | undefined, key: string, tone: "main" | "fb" | "shadow") => (
    <span key={key} style={{
      display: "inline-flex", gap: 4, alignItems: "center", padding: "2px 8px", borderRadius: 999,
      fontSize: 11, border: `1px solid ${A.line}`,
      background: tone === "main" ? A.line2 : A.card, color: tone === "shadow" ? A.muted2 : A.ink2,
    }}>
      <span style={{ fontWeight: tone === "main" ? 600 : 400 }}>{label}</span>
      {via && <span style={{ color: A.muted2 }}>· {via}</span>}
    </span>
  );
  return (
    <div data-testid="llm-route-line" style={{
      width: "100%", display: "flex", flexWrap: "wrap", alignItems: "center", gap: 6,
      fontSize: 11, color: A.muted2, paddingLeft: 2,
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

// ── Primary-model row (PATCH /llm-config/{stage}) + its route editor ───────────
function ModelRow({ row }: { row: StageConfigRow }) {
  const t = useT(MODELS);
  const { formatDateTime } = useFormat();
  const stageLabels = useLocalized({ en: STAGE_LABELS, vi: STAGE_LABELS_VI });
  const toast = useToast();
  const qc = useQueryClient();
  const [modelId, setModelId] = useState(row.model_id);
  const [accountRoute, setAccountRoute] = useState(row.account_route ?? "");
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState("");

  const dirty = modelId !== row.model_id || accountRoute !== (row.account_route ?? "");
  const modelLabel = (id: string) => {
    const o = row.options.find((x) => x.model_id === id);
    return o ? withVia(o.label, o.via) : id;
  };
  const awsAccounts = useAwsAccounts();
  const accountText = useAccountText();
  // A route outside the options (a non-Claude stage has none) still shows the account ID, never the key (AA-998).
  const acctLabel = (v: string) =>
    row.account_route_options.find((o: AccountOption) => o.value === v)?.label
      ?? accountName(v, awsAccounts, accountText);

  const mut = useMutation({
    mutationFn: (body: { model_id: string; account_route: string | null }) =>
      apiSend<StageConfigRow>(`/api/admin/llm-config/${row.stage}`, { method: "PATCH", body, admin: true }),
    onSuccess: () => {
      toast.success(t("modelSaved", row.stage));
      setError("");
      qc.invalidateQueries({ queryKey: ["llm-config"] });
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
    mut.mutate({ model_id: modelId, account_route: accountRoute || null });
  }

  const saving = mut.isPending;

  return (
    <div data-testid={`llm-model-row-${row.stage}`} style={{
      display: "flex", flexDirection: "column", gap: 0, padding: "10px 4px",
      borderBottom: `1px solid ${A.line2}`,
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <div style={{ minWidth: 200 }}>
          <div style={{ fontFamily: mono, fontSize: 12, color: A.ink, fontWeight: 600 }}>{row.stage}</div>
          <div style={{ fontSize: 11.5, color: A.muted2 }}>{stageLabels[row.stage] ?? STAGE_LABELS[row.stage] ?? row.stage}</div>
        </div>
        <Badge tone={ROLE_TONE[row.role] ?? "neutral"} dot={false}>{row.role}</Badge>

        <select
          data-testid={`llm-model-select-${row.stage}`}
          value={modelId}
          onChange={(e) => setModelId(e.target.value)}
          disabled={saving}
          style={{
            padding: "6px 10px", borderRadius: 7, border: `1px solid ${A.line}`,
            fontSize: 12.5, fontFamily: "inherit", color: A.body, background: A.card, minWidth: 260, maxWidth: "100%",
          }}
        >
          {row.options.map((o: ModelOption) => (
            <option key={o.model_id} value={o.model_id} disabled={!o.available}>
              {withVia(o.label, o.via)}{!o.available ? t("notAvailable") : ""}
            </option>
          ))}
        </select>
        {(() => {
          const opt = row.options.find((o) => o.model_id === modelId);
          return !opt?.available && opt?.reason ? (
            <span title={opt.reason} style={{ display: "inline-flex", color: A.amber }}>
              <AlertTriangle size={13} />
            </span>
          ) : null;
        })()}

        {row.account_route_options.length > 0 && (
          <select
            value={accountRoute}
            onChange={(e) => setAccountRoute(e.target.value)}
            disabled={saving}
            style={{
              padding: "6px 10px", borderRadius: 7, border: `1px solid ${A.line}`,
              fontSize: 12.5, fontFamily: "inherit", color: A.body, background: A.card, minWidth: 150, maxWidth: "100%",
            }}
          >
            {row.account_route_options.map((o: AccountOption) => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>
        )}

        <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 8 }}>
          {error && <span style={{ fontSize: 11.5, color: A.red }}>{error}</span>}
          <span style={{ fontSize: 10.5, color: A.muted2 }}>
            {row.updated_at ? formatDateTime(row.updated_at) : "—"} · {row.updated_by}
          </span>
          <Button variant="secondary" size="sm" disabled={!dirty || saving} onClick={() => setConfirming(true)}>
            {saving ? <Spinner size={12} /> : null}
            {saving ? t("saving") : t("save")}
          </Button>
        </div>
      </div>

      <RouteLine route={row.route} />

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
            <p style={{ fontSize: 12.5, color: A.muted, marginTop: 0, marginBottom: 12 }}>
              {t("changeModelBody")}
            </p>
            <div style={{
              background: A.line2, borderRadius: 8, padding: "10px 12px",
              fontSize: 12.5, fontFamily: mono, color: A.ink2,
            }}>
              {modelLabel(row.model_id)}{row.account_route ? ` (${acctLabel(row.account_route)})` : ""}
              {" → "}
              {modelLabel(modelId)}{accountRoute ? ` (${acctLabel(accountRoute)})` : ""}
            </div>
          </div>
        }
      />
    </div>
  );
}

// ── Jev stages (read-only, same as before) ────────────────────────────────────
interface JevQuestionRow {
  question_key: string; stage: string; kind: string; mode: "off" | "shadow" | "enforce";
  accept_floor: number | null; reject_ceiling: number | null; calibration_ref: string | null;
  verdicts: number; acted: number; cost_usd: number;
}
interface JevSummaryResp { questions?: JevQuestionRow[] }

function JevStagesCard() {
  const t = useT(MODELS);
  const { data } = useQuery({
    queryKey: ["decisions-summary", 7],
    queryFn: () => apiGet<JevSummaryResp>("/api/admin/decisions/summary?days=7", { admin: true }).catch(() => ({ questions: [] } as JevSummaryResp)),
  });
  const rows = data?.questions ?? null;
  const modeTone = { off: "neutral", shadow: "info", enforce: "accent" } as const;
  return (
    <Card>
      <SLabel>{t("jevTitle")}</SLabel>
      <p style={{ fontSize: 12, color: A.muted2, margin: "0 0 10px" }}>
        {t("jevIntroA")} <b>{t("jevIntroB")}</b> {t("jevIntroC")}{" "}
        <a href="/admin/decisions" style={{ color: A.gold }}>{t("jevDecisions")}</a>.
      </p>
      {rows === null ? <Spinner /> : rows.length === 0 ? (
        <div style={{ fontSize: 12.5, color: A.muted }}>{t("noJevQuestions")}</div>
      ) : (
        <ScrollX>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
          <tbody>
            {rows.map((q) => (
              <tr key={q.question_key} style={{ borderTop: `1px solid ${A.line}` }}>
                <td style={{ padding: "6px 0", fontFamily: mono }}>{q.stage}</td>
                <td style={{ padding: "6px 8px", fontFamily: mono }}>{q.question_key}</td>
                <td style={{ padding: "6px 8px" }}><Badge tone={modeTone[q.mode]} dot={false}>{q.mode}</Badge></td>
                <td style={{ padding: "6px 8px", fontFamily: mono }}>
                  {q.accept_floor ?? "—"} / {q.reject_ceiling ?? "—"}
                </td>
                <td style={{ padding: "6px 8px", color: A.muted }}>{q.calibration_ref ? t("calibrated") : t("notCalibrated")}</td>
                <td style={{ padding: "6px 8px", fontFamily: mono }}>{t("verdictsActed", q.verdicts, q.acted)}</td>
                <td style={{ padding: "6px 0", fontFamily: mono, textAlign: "right" }}>${q.cost_usd.toFixed(5)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        </ScrollX>
      )}
    </Card>
  );
}

// ── Stages sub-tab ─────────────────────────────────────────────────────────────
function StagesSubTab() {
  const t = useT(MODELS);
  const groupLabels = useLocalized({ en: STAGE_GROUP_LABELS, vi: STAGE_GROUP_LABELS_VI });
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ["llm-config"],
    queryFn: () => apiGet<LlmConfigResp>("/api/admin/llm-config", { admin: true }),
  });

  const rows = useMemo(() => data?.stages ?? [], [data]);

  if (isLoading && !data) return <LoadingScreen msg={t("loadingConfig")} />;
  if (isError || !data) {
    return (
      <Card style={{ textAlign: "center", padding: 40 }}>
        <div style={{ color: A.red, marginBottom: 12 }}>{t("loadConfigError")}</div>
        <Button variant="secondary" onClick={() => refetch()}>{t("retry")}</Button>
      </Card>
    );
  }

  const byStage = Object.fromEntries(rows.map((r) => [r.stage, r]));
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <LlmAvailabilityCard />
      <p style={{ fontSize: 12, color: A.muted2, margin: 0 }}>
        {t("stagesIntroA")} <b>{t("stagesIntroB")}</b>{t("stagesIntroC")} <b>{t("stagesIntroD")}</b>{" "}
        {t("stagesIntroE")}
      </p>
      {STAGE_GROUPS.map((group) => {
        const stages = group.stages.filter((s) => !HIDDEN_STAGES.has(s) && byStage[s]);
        if (stages.length === 0) return null;
        return (
          <Card key={group.key}>
            <SLabel>{groupLabels[group.key] ?? group.label}</SLabel>
            <div>
              {stages.map((stage) => (
                <ModelRow key={stage} row={byStage[stage]} />
              ))}
            </div>
          </Card>
        );
      })}
      <JevStagesCard />
    </div>
  );
}

// ── Main tab (sub-tab switcher) ────────────────────────────────────────────────
const SUB_TABS = [
  { key: "stages", label: "subStages" },
  { key: "catalog", label: "subCatalog" },
  { key: "shadow", label: "subShadow" },
] as const;

function ModelsTabInner() {
  const t = useT(MODELS);
  const [sub, setSub] = useState("stages");
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <div data-testid="llm-models-ready">
        <Tabs tabs={SUB_TABS.map((s) => ({ key: s.key, label: t(s.label) }))} active={sub} onChange={setSub} />
      </div>
      {sub === "stages" && <StagesSubTab />}
      {sub === "catalog" && <CatalogTab />}
      {sub === "shadow" && <ShadowReportTab />}
    </div>
  );
}

export function ModelsTab() {
  return (
    <ToastProvider>
      <ModelsTabInner />
    </ToastProvider>
  );
}
