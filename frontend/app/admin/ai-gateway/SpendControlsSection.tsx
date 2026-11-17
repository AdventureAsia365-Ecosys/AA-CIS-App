"use client";
// app/admin/ai-gateway/SpendControlsSection.tsx — AA-1126 (S283): the Spend controls section of AI gateway › Budgets.
//
// One read, GET /api/admin/ai-gateway/spend-controls (backend api/services/ai_gateway_spend_controls.py):
//   Settings in force   the connection cap mode, the anomaly breaker and the run caps of this environment, from the
//                       settings registry (Platform › Settings, app CIS, area Spend). Shown here, never edited here:
//                       each row links to its row on Platform › Settings, the one place they change (approved,
//                       audited), so the page and the gateway read the same value.
//   Anomaly breaker     each app's spend today against its trip line; Reset (ai.admin, not a tighten-only role;
//                       POST /api/admin/ai-gateway/anomaly-breaker/{app}/reset, confirm first, audited).
//   useAtCapText        the Budgets tab's connection tier says "Fails over at the cap" under failover_continue.
//   useRunCapSetting    one source per cap (review S283): the central caps of job:s1_rewrite / eval / adhoc show their
//                       per-run cap as the Platform › Settings value (RunCapCell, RunCapEditorNote), never a 2nd value.
// Built on the kit only (Section, DataTable, Badge, Button, ConfirmModal, DateText, ErrorState, Toast).

import Link from "next/link";
import { useCallback, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RotateCcw } from "lucide-react";
import {
  Badge, Button, ConfirmModal, DataTable, DateText, ErrorState, K, Section, apiGet, apiSend, mono, useT, useToast,
  type ColumnDef,
} from "../../_kit";
import { usd } from "@/lib/aiGateway";
import {
  SPEND_CONTROLS_API, SPEND_CONTROLS_KEY, SPEND_SETTINGS_HREF, anomalyResetPath, breakerState, breakerTone,
  changedFromDefault, connectionAtCap, mayReset, runCapSettingOf, settingValueText, type AnomalyVerdict,
  type SpendControlSetting,
  type SpendControlsResp,
} from "@/lib/spendControls";
import { BUDGETS } from "./budgets.messages";
import { PROFILE_APP } from "./appProfiles.messages";
import { BREAKER_STATE, SPEND_CONTROLS, VALUE_SOURCE } from "./spendControls.messages";
import { usePageAccess } from "./usePageAccess";
import { useRefusalText } from "./useRefusalText";

const small = { fontSize: 12, color: K.muted, lineHeight: 1.45 } as const;
const TABLE_MIN_WIDTH = 760;

function useSpendControls() {
  return useQuery({
    queryKey: SPEND_CONTROLS_KEY,
    queryFn: () => apiGet<SpendControlsResp>(SPEND_CONTROLS_API, { admin: true }),
    refetchInterval: 120_000,
    refetchIntervalInBackground: false,
    retry: false,
  });
}

/** AA-1126 G1: the "at the cap" text of a connection (Budgets › LLM connection budgets): its own cost policy and the
 *  environment's connection cap mode (one read shared with this section; before it loads, today's wording). */
export function useAtCapText(): (hardStop: boolean) => string {
  const t = useT(BUDGETS);
  const tsc = useT(SPEND_CONTROLS);
  const mode = useSpendControls().data?.connection_cap_mode;
  return useCallback((hardStop: boolean) => {
    const at = connectionAtCap(hardStop, mode);
    return at === "fails_over" ? tsc("capFailsOver") : at === "stops" ? t("stopsAtCap") : t("alertOnly");
  }, [mode, t, tsc]);
}

/** The setting that is the per-run cap of a central budget scope (null: none, or not loaded yet). */
export function useRunCapSetting(): (scope: string | null | undefined) => SpendControlSetting | null {
  const data = useSpendControls().data;
  return useCallback((scope) => runCapSettingOf(data, scope), [data]);
}

/** The per-run cell of a central cap: for a scope whose per-run cap is a setting, its value and the link that edits
 *  it; else ``otherwise`` (the row's own per-run amount). */
export function RunCapCell({ scope, otherwise }: { scope: string; otherwise: React.ReactNode }) {
  const t = useT(SPEND_CONTROLS);
  const setting = useRunCapSetting()(scope);
  if (!setting) return <>{otherwise}</>;
  return (
    <div style={{ display: "grid", gap: 2, justifyItems: "end" }}>
      <span style={{ fontFamily: mono }}>{settingValueText(setting.value, setting)}</span>
      <Link href={setting.href} style={{ fontSize: 12 }} onClick={(e) => e.stopPropagation()}>
        {t("runCapFromSettings")}</Link>
    </div>
  );
}

/** The central cap editor's per-run rule for its scope: the setting that is its per-run cap (null: none), whether the
 *  editor shows a per-run field (a job scope without such a setting) and the error of a per-run amount it sends anyway
 *  ("" when none). */
export function useRunCapRule(scope: string, kind: string, body: { per_run_usd: number | null } | undefined) {
  const t = useT(SPEND_CONTROLS);
  const setting = useRunCapSetting()(scope);
  const sendsRun = body !== undefined && body.per_run_usd !== null;
  return { setting, perRunField: kind === "job" && !setting, error: setting && sendsRun ? t("errRunSetting") : "" };
}

/** The central cap editor's line in place of the per-run field for such a scope (nothing for any other scope). */
export function RunCapEditorNote({ setting }: { setting: SpendControlSetting | null }) {
  const t = useT(SPEND_CONTROLS);
  if (!setting) return null;
  return (
    <div role="note" style={small} data-testid="central-run-cap-setting">
      {t("runCapEditorNote", settingValueText(setting.value, setting))}{" "}
      <Link href={setting.href}>{t("runCapFromSettings")}</Link>
    </div>
  );
}

function useAppName(): (app: string) => string {
  const ta = useT(PROFILE_APP);
  return (app) => (app in PROFILE_APP.en ? ta(app as keyof typeof PROFILE_APP.en) : app);
}

function SettingsTable({ rows, loading }: { rows: SpendControlSetting[]; loading: boolean }) {
  const t = useT(SPEND_CONTROLS);
  const ts = useT(VALUE_SOURCE);
  const columns = useMemo<ColumnDef<SpendControlSetting>[]>(() => [
    { accessorKey: "label", header: t("colSetting"), enableSorting: false,
      cell: (x) => (
        <div style={{ display: "grid", gap: 2, minWidth: 0 }}>
          <strong style={{ color: K.ink }}>{x.row.original.label}</strong>
          <span style={{ ...small, fontFamily: mono }}>{x.row.original.key}</span>
        </div>
      ) },
    { id: "value", header: t("colValue"), enableSorting: false,
      cell: (x) => (
        <span style={{ fontFamily: mono, color: K.ink, whiteSpace: "nowrap" }}>
          {settingValueText(x.row.original.value, x.row.original)}</span>
      ) },
    { id: "default", header: t("colDefault"), enableSorting: false,
      cell: (x) => (
        <span style={{ ...small, fontFamily: mono, whiteSpace: "nowrap" }}>
          {settingValueText(x.row.original.default_value, x.row.original)}</span>
      ) },
    { accessorKey: "source", header: t("colSource"), enableSorting: false,
      cell: (x) => {
        const s = x.row.original;
        const known = s.source in VALUE_SOURCE.en;
        return (
          <Badge tone={changedFromDefault(s) ? "info" : "neutral"} dot={false} style={{ whiteSpace: "nowrap" }}>
            {known ? ts(s.source as keyof typeof VALUE_SOURCE.en) : s.source}</Badge>
        );
      } },
    { id: "edit", header: "", enableSorting: false, meta: { align: "right" },
      cell: (x) => (
        <Link href={x.row.original.href} style={{ fontSize: 13 }} data-testid={`spend-control-edit-${x.row.original.key}`}>
          {t("edit")}</Link>
      ) },
  ], [t, ts]);
  return (
    <DataTable<SpendControlSetting> tableId="ai-gateway-spend-controls" data={rows} columns={columns}
      getRowId={(r) => r.key} loading={loading} searchable={false} paginate={false} stickyHeader={false}
      tableMinWidth={TABLE_MIN_WIDTH} emptyTitle="—" />
  );
}

function BreakerTable({ apps, loading, canReset, onReset }: {
  apps: AnomalyVerdict[]; loading: boolean; canReset: (v: AnomalyVerdict) => boolean;
  onReset: (v: AnomalyVerdict) => void;
}) {
  const t = useT(SPEND_CONTROLS);
  const tb = useT(BREAKER_STATE);
  const appName = useAppName();
  const columns = useMemo<ColumnDef<AnomalyVerdict>[]>(() => [
    { accessorKey: "app", header: t("colApp"), enableSorting: false,
      cell: (x) => <strong style={{ color: K.ink }}>{appName(x.row.original.app)}</strong> },
    { id: "today", header: t("colToday"), enableSorting: false, meta: { align: "right" },
      cell: (x) => <span style={{ fontFamily: mono, color: K.ink }}>{usd(x.row.original.spent_usd, 2)}</span> },
    { id: "line", header: t("colLine"), enableSorting: false, meta: { align: "right" },
      cell: (x) => {
        const v = x.row.original;
        return (
          <div style={{ display: "grid", gap: 2, minWidth: 0, justifyItems: "end" }}>
            <span style={{ fontFamily: mono, color: K.ink }}>{usd(v.threshold_usd, 2)}</span>
            <span style={small}>{t("lineOf", String(v.multiple), usd(v.floor_usd, 2))}</span>
          </div>
        );
      } },
    { id: "median", header: t("colMedian"), enableSorting: false, meta: { align: "right" },
      cell: (x) => <span style={{ ...small, fontFamily: mono }}>{usd(x.row.original.median_usd, 2)}</span> },
    { id: "state", header: t("colState"), enableSorting: false,
      cell: (x) => {
        const state = breakerState(x.row.original);
        return <Badge tone={breakerTone(state)} dot={false} style={{ whiteSpace: "nowrap" }}>{tb(state)}</Badge>;
      } },
    { id: "reset_at", header: t("colReset"), enableSorting: false,
      cell: (x) => (x.row.original.reset_at
        ? <span style={small}><DateText value={x.row.original.reset_at} relative /></span>
        : <span style={small}>—</span>) },
    { id: "actions", header: "", enableSorting: false, meta: { align: "right" },
      cell: (x) => (canReset(x.row.original) ? (
        <Button variant="ghost" size="sm" onClick={() => onReset(x.row.original)}
          testId={`anomaly-reset-${x.row.original.app}`}><RotateCcw size={14} /> {t("reset")}</Button>
      ) : null) },
  ], [t, tb, appName, canReset, onReset]);
  return (
    <DataTable<AnomalyVerdict> tableId="ai-gateway-anomaly-breaker" data={apps} columns={columns}
      getRowId={(r) => r.app} loading={loading} searchable={false} paginate={false} stickyHeader={false}
      tableMinWidth={TABLE_MIN_WIDTH} emptyTitle="—" />
  );
}

/** The breaker reset: confirm first, then POST; the toast names the app. */
function useBreakerReset() {
  const t = useT(SPEND_CONTROLS);
  const appName = useAppName();
  const refusal = useRefusalText();
  const toast = useToast();
  const qc = useQueryClient();
  const [resetting, setResetting] = useState<AnomalyVerdict | null>(null);
  const mut = useMutation({
    mutationFn: (app: string) => apiSend(anomalyResetPath(app), { method: "POST", admin: true }),
    onSuccess: (_d, app) => {
      toast.success(t("resetDone", appName(app)));
      setResetting(null);
      qc.invalidateQueries({ queryKey: [...SPEND_CONTROLS_KEY] });
    },
    onError: (e) => {
      setResetting(null);
      toast.error(refusal(e));
    },
  });
  const modal = (
    <ConfirmModal open={resetting !== null} onClose={() => setResetting(null)}
      onConfirm={() => resetting && mut.mutate(resetting.app)}
      title={resetting ? t("resetTitle", appName(resetting.app)) : ""} body={t("resetBody")}
      confirmLabel={t("resetConfirm")} busy={mut.isPending} destructive testId="anomaly-reset-confirm" />
  );
  return { ask: setResetting, modal };
}

function Notes({ data }: { data: SpendControlsResp | undefined }) {
  const t = useT(SPEND_CONTROLS);
  if (!data) return null;
  return (
    <>
      {data.missing.length > 0 && (
        <div role="status" style={{ ...small, padding: "0 16px" }}>{t("missing", data.missing.length)}</div>
      )}
      {data.errors.length > 0 && (
        <div role="status" style={{ ...small, padding: "0 16px", color: K.danger }}>{t("sourceErrors")}</div>
      )}
    </>
  );
}

function BreakerPart({ data, onReset }: { data: SpendControlsResp | undefined; onReset: (v: AnomalyVerdict) => void }) {
  const t = useT(SPEND_CONTROLS);
  const access = usePageAccess();
  const anomaly = data?.anomaly;
  const on = !!anomaly && anomaly.mode !== "off";
  const canReset = (v: AnomalyVerdict) => mayReset(v, access.can("anomaly_reset"), access.tightenOnly);
  return (
    <>
      <div style={{ padding: "4px 16px 0", display: "grid", gap: 4 }}>
        <strong style={{ color: K.ink, fontSize: 14 }}>{t("breakerTitle")}</strong>
        <span style={small}>{on ? t("breakerHelp", anomaly.mode) : t("breakerOff")}</span>
        {on && <span style={small}>{t("breakerEnforcedNote")}</span>}
        {access.tightenOnly && <span style={small}>{t("resetTighten")}</span>}
      </div>
      {on && <BreakerTable apps={anomaly.apps} loading={!data} canReset={canReset} onReset={onReset} />}
    </>
  );
}

export default function SpendControlsSection() {
  const t = useT(SPEND_CONTROLS);
  const q = useSpendControls();
  const reset = useBreakerReset();
  const data = q.data;
  return (
    <Section title={t("title")} description={t("help")} padded={false} testId="ai-gateway-spend-controls"
      actions={<Link href={data?.settings_href ?? SPEND_SETTINGS_HREF} style={{ fontSize: 13 }}>{t("editAll")}</Link>}>
      {q.isError ? (
        <ErrorState message={`${t("loadError")}: ${(q.error as Error).message}`} onRetry={() => q.refetch()} />
      ) : (
        <div style={{ display: "grid", gap: 12, minWidth: 0 }}>
          <SettingsTable rows={data?.settings ?? []} loading={!data} />
          <div style={{ ...small, padding: "0 16px" }}>{t("runCapNote")}</div>
          <Notes data={data} />
          <BreakerPart data={data} onReset={reset.ask} />
        </div>
      )}
      {reset.modal}
    </Section>
  );
}
