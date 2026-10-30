"use client";
// app/admin/writing-memory/MetricsTab.tsx — AA-922 WM-31: Writing memory › Metrics (app ADR 0010, S245 audit §8, D15).
//
// What it shows (the newest stored weekly report, or a dry run computed on request):
//   KPIs          per writer: writes, re-flag with its Wilson 95% interval, repeat errors, light-edit share, minutes
//                 to fix, tenant edits per write, reason capture (target 80%), cost per write, the replay hit rate and
//                 the clean-pairs counter (the fine-tune option stays visible; there is no export path). "—" = not
//                 measured, never 0.
//   Soft stops    per active release with content: the codes that fired, what the job did (rolled back, kept, …) and
//                 Keep: a reason (and the codes, or all) that stops the next run from taking it out of service.
//   Rules / stop  expired and re-confirmed rules, and the project stop rule (two releases without improvement).
//   Queue         the 5-10 weekly cases where the judge or the gates and a human disagree, resolved in one click.
// Header actions: Measure now (dry run: nothing stored or moved) and Run the weekly job now (queues the job).
// Every action is a backend route (api/routers/admin_writing_memory.py, ai.admin); the buttons show only for a role
// with ai.admin. Admin only: tenants never see any of this (memory: tenant sees content only).

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Gauge, Play } from "lucide-react";
import { useMemo, useState } from "react";
import {
  Badge, Button, DataTable, ErrorState, Field, Input, K, KpiTile, Modal, TableSkeleton, apiGet, mono,
  useAdminIdentity, useT, useToast, type ColumnDef,
} from "../../_kit";
import { can } from "@/lib/rbac";
import {
  SOFT_STOP_CODES, WM_API, WRITERS, actionTone, pctOrDash, usd,
  type Disagreement, type KpiAction, type KpiReport, type MetricsPage, type ReleaseStops, type Resolution,
  type SoftStopCode, type Writer, type WriterBlock,
} from "@/lib/writingMemory";
import { MX } from "./metrics.messages";
import { useWmText } from "./useWmText";

type KpiRow = { writer: Writer; b: WriterBlock };

function H({ children }: { children: React.ReactNode }) {
  return <h2 style={{ fontSize: 14, margin: "6px 0 6px" }}>{children}</h2>;
}

function KeepModal({ target, onClose }: { target: ReleaseStops | null; onClose: () => void }) {
  const t = useT(MX);
  const toast = useToast();
  const qc = useQueryClient();
  const wm = useWmText();
  const [reason, setReason] = useState("");
  const [days, setDays] = useState("14");
  const [busy, setBusy] = useState(false);
  const codes = (target?.stops ?? []).map((s) => s.code);

  async function save() {
    if (!target) return;
    setBusy(true);
    const r = await wm.send(`/releases/${target.release_id}/keep`, "POST",
      { reason: reason.trim(), codes, days: Number(days) || 14 });
    setBusy(false);
    if (r.ok) {
      toast.success(t("keepSaved"));
      qc.invalidateQueries({ queryKey: ["wm"] });
      setReason("");
      onClose();
    } else toast.error(r.message);
  }

  return (
    <Modal open={!!target} onClose={onClose} title={target ? t("keepTitle", target.release_no) : ""}
      footer={<Button variant="primary" onClick={save} disabled={busy || reason.trim().length < 3}>{t("keep")}</Button>}>
      <div style={{ display: "grid", gap: 10, fontSize: 13 }}>
        <div style={{ fontFamily: mono, fontSize: 12 }}>{codes.length ? codes.join(", ") : t("keepAll")}</div>
        <Field label={t("keepReason")} required>
          <Input value={reason} onChange={(e) => setReason(e.target.value)} maxLength={500} />
        </Field>
        <Field label={t("keepDays")}>
          <Input value={days} onChange={(e) => setDays(e.target.value.replace(/[^0-9]/g, ""))} inputMode="numeric" />
        </Field>
      </div>
    </Modal>
  );
}

function Queue({ canAdmin }: { canAdmin: boolean }) {
  const t = useT(MX);
  const toast = useToast();
  const qc = useQueryClient();
  const wm = useWmText();
  const q = useQuery({ queryKey: ["wm", "disagreements"],
    queryFn: () => apiGet<{ items: Disagreement[] }>(`${WM_API}/disagreements?status=open`) });
  const [busy, setBusy] = useState<string | null>(null);

  async function resolve(d: Disagreement, resolution: Resolution) {
    setBusy(d.item_id);
    const r = await wm.send(`/disagreements/${d.item_id}/resolve`, "POST", { resolution });
    setBusy(null);
    if (r.ok) {
      toast.success(t("resolved"));
      qc.invalidateQueries({ queryKey: ["wm", "disagreements"] });
    } else toast.error(r.message);
  }

  const cols = useMemo<ColumnDef<Disagreement, unknown>[]>(() => [
    { accessorKey: "week_start", header: t("colWeek") },
    { accessorKey: "writer", header: t("colWriter"),
      cell: (c) => <span style={{ fontFamily: mono }}>{c.row.original.writer}</span> },
    { accessorKey: "source", header: t("colSource"),
      cell: (c) => <span>{c.row.original.source} <span style={{ fontFamily: mono, color: K.muted }}>
        {c.row.original.source_ref.slice(0, 8)}</span></span> },
    { accessorKey: "machine_verdict", header: t("colMachine") },
    { accessorKey: "human_label", header: t("colHuman") },
    { id: "act", header: "", enableSorting: false,
      cell: (c) => canAdmin ? (
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap", justifyContent: "flex-end" }}>
          {(["judge_right", "human_right", "unclear"] as const).map((res) => (
            <Button key={res} size="sm" variant={res === "unclear" ? "ghost" : "secondary"}
              disabled={busy === c.row.original.item_id} onClick={() => resolve(c.row.original, res)}>
              {t(res === "judge_right" ? "judgeRight" : res === "human_right" ? "humanRight" : "unclear")}</Button>
          ))}
        </div>) : null },
  // eslint-disable-next-line react-hooks/exhaustive-deps
  ], [t, canAdmin, busy]);

  return (
    <section>
      <H>{t("queueTitle")}</H>
      <div style={{ fontSize: 12.5, color: K.muted, marginBottom: 8 }}>{t("queueHelp")}</div>
      {q.error ? <ErrorState message={(q.error as Error).message} onRetry={() => q.refetch()} /> : (
        <DataTable tableId="wm-disagreements" data={q.data?.items ?? []} columns={cols}
          getRowId={(d) => d.item_id} loading={q.isLoading} paginate={false} compact tableMinWidth={760}
          emptyTitle={t("queueEmpty")} />
      )}
    </section>
  );
}

export default function MetricsTab() {
  const t = useT(MX);
  const toast = useToast();
  const qc = useQueryClient();
  const wm = useWmText();
  const canAdmin = can(useAdminIdentity().role, "ai.admin");
  const list = useQuery({ queryKey: ["wm", "metrics"], queryFn: () => apiGet<MetricsPage>(`${WM_API}/metrics`) });
  const [dry, setDry] = useState<KpiReport | null>(null);
  const [busy, setBusy] = useState(false);
  const [keeping, setKeeping] = useState<ReleaseStops | null>(null);
  const report = dry ?? list.data?.items[0] ?? null;

  async function measure() {
    setBusy(true);
    const r = await wm.send<KpiReport>("/metrics/run", "POST", { dry_run: true });
    setBusy(false);
    if (r.ok) {
      setDry(r.data);
      toast.info(t("dryDone", r.data.soft_stops.reduce((n, s) => n + s.stops.length, 0)));
    } else toast.error(r.message);
  }

  async function runNow() {
    setBusy(true);
    const r = await wm.send<{ job_id: string }>("/metrics/run", "POST", { dry_run: false });
    setBusy(false);
    if (r.ok) {
      toast.success(t("queued", r.data.job_id.slice(0, 8)));
      qc.invalidateQueries({ queryKey: ["wm", "metrics"] });
    } else toast.error(r.message);
  }

  const kpiRows: KpiRow[] = report ? WRITERS.map((w) => ({ writer: w, b: report.kpis.writers[w] })).filter((r) => r.b)
    : [];
  const kpiCols = useMemo<ColumnDef<KpiRow, unknown>[]>(() => [
    { accessorKey: "writer", header: t("colWriter"), cell: (c) => <span style={{ fontFamily: mono }}>{c.row.original.writer}</span> },
    { id: "writes", accessorFn: (r) => r.b.overall.writes, header: t("colWrites") },
    { id: "reflag", accessorFn: (r) => r.b.overall.reflag_rate ?? -1, header: t("colReflag"),
      cell: (c) => { const o = c.row.original.b.overall;
        return <span>{pctOrDash(o.reflag_rate)}{o.reflag_ci95 && <span style={{ color: K.muted, fontSize: 11.5 }}>
          {" "}({pctOrDash(o.reflag_ci95[0], 0)}–{pctOrDash(o.reflag_ci95[1], 0)})</span>}</span>; } },
    { id: "repeat", accessorFn: (r) => r.b.overall.repeat_error_rate ?? -1, header: t("colRepeat"),
      cell: (c) => pctOrDash(c.row.original.b.overall.repeat_error_rate) },
    { id: "light", accessorFn: (r) => r.b.overall.light_edit_share ?? -1, header: t("colLight"),
      cell: (c) => pctOrDash(c.row.original.b.overall.light_edit_share) },
    { id: "minutes", accessorFn: (r) => r.b.overall.minutes_to_fix ?? -1, header: t("colMinutes"),
      cell: (c) => c.row.original.b.overall.minutes_to_fix ?? "—" },
    { id: "tenant", accessorFn: (r) => r.b.overall.tenant_edit_rate ?? -1, header: t("colTenantEdits"),
      cell: (c) => pctOrDash(c.row.original.b.overall.tenant_edit_rate) },
    { id: "capture", accessorFn: (r) => r.b.overall.capture_complete ?? -1, header: t("colCapture"),
      cell: (c) => { const o = c.row.original.b.overall;
        return <Badge tone={o.capture_ok === null ? "neutral" : o.capture_ok ? "success" : "warning"} dot={false}>
          {pctOrDash(o.capture_complete, 0)}</Badge>; } },
    { id: "cost", accessorFn: (r) => r.b.overall.cost_per_write ?? -1, header: t("colCost"),
      cell: (c) => (c.row.original.b.overall.cost_per_write === null ? "—" : usd(c.row.original.b.overall.cost_per_write)) },
    { id: "replay", accessorFn: (r) => r.b.replay.rate ?? -1, header: t("colReplay"),
      cell: (c) => pctOrDash(c.row.original.b.replay.rate) },
    { id: "pairs", accessorFn: (r) => r.b.clean_pairs, header: t("colCleanPairs") },
  ], [t]);

  const actionOf = (r: ReleaseStops): KpiAction | undefined => report?.actions.find((a) => a.release_id === r.release_id);
  const stopCols = useMemo<ColumnDef<ReleaseStops, unknown>[]>(() => [
    { id: "release", accessorFn: (r) => `${r.writer} #${r.release_no}`, header: t("colRelease"),
      cell: (c) => <span style={{ fontFamily: mono }}>{c.row.original.writer} #{c.row.original.release_no}{" "}
        {wm.owner(c.row.original.scope, c.row.original.tenant_id)}</span> },
    { id: "stops", header: t("colStops"), enableSorting: false,
      cell: (c) => <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
        {c.row.original.stops.map((s) => <Badge key={s.code} tone="danger" dot={false}>{s.code}</Badge>)}
        {c.row.original.kept.map((s) => <Badge key={`k-${s.code}`} tone="warning" dot={false}>{s.code}</Badge>)}
        {!c.row.original.stops.length && !c.row.original.kept.length && "—"}</div> },
    { id: "action", header: t("colAction"), enableSorting: false,
      cell: (c) => { const a = actionOf(c.row.original);
        return a ? <Badge tone={actionTone(a.action)} dot={false}>{a.action}</Badge> : "—"; } },
    { id: "keep", header: "", enableSorting: false,
      cell: (c) => canAdmin && c.row.original.stops.length ? (
        <Button size="sm" variant="secondary" onClick={() => setKeeping(c.row.original)}>{t("keep")}</Button>) : null },
  // eslint-disable-next-line react-hooks/exhaustive-deps
  ], [t, canAdmin, wm, report]);

  const stopCodes = new Set<SoftStopCode>(SOFT_STOP_CODES);
  const spend = report?.kpis.learning_spend;
  return (
    <div style={{ display: "grid", gap: 16, minWidth: 0 }}>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", justifyContent: "space-between" }}>
        <div style={{ fontSize: 12.5, color: K.muted, maxWidth: 760 }}>{t("help")}</div>
        {canAdmin && (
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
            <Button variant="ghost" onClick={measure} disabled={busy} title={t("runDryTitle")}>
              <Gauge size={14} /> {t("runDry")}</Button>
            <Button onClick={runNow} disabled={busy} title={t("runNowTitle")}><Play size={14} /> {t("runNow")}</Button>
          </div>
        )}
      </div>
      {list.error ? <ErrorState message={(list.error as Error).message} onRetry={() => list.refetch()} />
        : list.isLoading ? <TableSkeleton rows={4} cols={6} />
        : !report ? <div style={{ fontSize: 13, color: K.muted }} data-testid="wm-metrics-empty">{t("noReport")}</div>
        : (
          <>
            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <strong style={{ fontSize: 14 }}>{t("week", report.week_start)}</strong>
              {dry && <Badge tone="warning" dot={false}>{t("dryRunBadge")}</Badge>}
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))", gap: 10 }}>
              <KpiTile size="sm" label={t("spend")} value={usd(spend?.month_spent_usd ?? null)}
                hint={`/ ${usd(spend?.month_cap_usd ?? 25)}`} />
            </div>
            <DataTable tableId="wm-kpis" data={kpiRows} columns={kpiCols} getRowId={(r) => r.writer}
              paginate={false} compact tableMinWidth={1100} emptyTitle={t("noReport")} />
            <div style={{ fontSize: 12, color: K.muted }}>{t("cleanPairsHelp")}</div>
            <section>
              <H>{t("softStopsTitle")}</H>
              <DataTable tableId="wm-soft-stops" data={report.soft_stops.filter((s) => s.stops.every((x) => stopCodes.has(x.code)))}
                columns={stopCols} getRowId={(r) => r.release_id} paginate={false} compact tableMinWidth={760}
                emptyTitle={t("noSoftStops")} />
            </section>
            <div style={{ fontSize: 12.5, display: "grid", gap: 4 }}>
              <div>{t("rulesLine", report.rules.expired.length, report.rules.reconfirmed.length)}</div>
              <div style={{ color: report.project_stop.flagged.length ? K.danger : K.muted }}>
                {report.project_stop.flagged.length ? t("projectStop", report.project_stop.flagged.join(", "))
                  : t("projectOk")}</div>
            </div>
          </>
        )}
      <Queue canAdmin={canAdmin} />
      <KeepModal target={keeping} onClose={() => setKeeping(null)} />
    </div>
  );
}
