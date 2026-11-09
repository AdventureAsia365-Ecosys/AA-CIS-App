"use client";
// app/admin/tenants/_components/Tenant360Quality.tsx
// AA-766 — Tenant 360 Quality tab: T10 gate verdicts per gate (pass / blocking fail / warn / held
// pieces), the piece status mix, held reasons and the tenant edit rate on T2 tour versions. From
// GET /admin/tenants/{id}/quality. The per-channel gate telemetry AA-666 showed stays below it.
// Admin-only: the tenant sees content, never these verdicts (AA-613). AA-994: text in the UI language.

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import {
  K, mono, DataTable, ErrorState, KpiTile, Spinner, apiGet, useT, type Translator, ChipButton,
} from "../../../_kit";
import { type TenantQuality, pct } from "./tenant360";
import { T360 } from "./tenant360.messages";

const WINDOWS = [7, 30, 90] as const;
type GateRow = TenantQuality["gates"][number];
const tabular: React.CSSProperties = { fontVariantNumeric: "tabular-nums" };
function count(color: string) {
  return function CountCell(c: { getValue: () => unknown }) {
    const n = Number(c.getValue());
    return <span style={{ ...tabular, color: n > 0 ? color : K.muted }}>{n}</span>;
  };
}

const gateColumns = (t: Translator<typeof T360.en>): ColumnDef<GateRow, unknown>[] => [
  { accessorKey: "gate", header: t("colGate"), cell: c => <span style={{ fontFamily: mono, fontSize: 11.5 }}>{String(c.getValue())}</span> },
  { accessorKey: "evaluated", header: t("colEvaluated"), cell: c => <span style={tabular}>{Number(c.getValue())}</span> },
  { accessorKey: "passed", header: t("colPassed"), cell: count(K.success) },
  { accessorKey: "pass_rate", header: t("colPassRate"), cell: c => <strong style={tabular}>{pct(c.getValue() as number | null)}</strong> },
  { accessorKey: "failed_blocking", header: t("colBlocking"), cell: count(K.danger) },
  { accessorKey: "failed_warn", header: t("colWarn"), cell: count(K.amber) },
  { accessorKey: "held_pieces", header: t("colHeld"), cell: count(K.amber) },
];

function H({ children }: { children: React.ReactNode }) {
  return <div style={{ fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: K.muted, margin: "18px 0 8px" }}>{children}</div>;
}

export function Tenant360Quality({ tenantId, byChannel }: { tenantId: string; byChannel?: React.ReactNode }) {
  const t = useT(T360);
  const [days, setDays] = useState<number>(30);
  const q = useQuery({
    queryKey: ["tenant360", tenantId, "quality", days],
    queryFn: () => apiGet<TenantQuality>(`/api/admin/tenants/${tenantId}/quality?days=${days}`, { admin: true }),
  });

  return (
    <div style={{ minWidth: 0 }}>
      <div style={{ display: "flex", gap: 6, marginBottom: 12, alignItems: "center", flexWrap: "wrap" }}>
        <span style={{ fontSize: 11.5, color: K.muted }}>{t("window")}</span>
        {WINDOWS.map(d => (
          <ChipButton key={d} size="sm" selected={days === d} onClick={() => setDays(d)}>{t("days", d)}</ChipButton>
        ))}
      </div>

      {q.isLoading ? (
        <div style={{ fontSize: 12, color: K.muted, display: "flex", gap: 6, alignItems: "center" }}><Spinner size={12} /> {t("loadingQuality")}</div>
      ) : q.isError || !q.data ? (
        <ErrorState message={t("qualityLoadError", (q.error as Error)?.message ?? t("unknownError"))} onRetry={() => q.refetch()} />
      ) : (
        <QualityBody data={q.data} />
      )}

      {byChannel && (<><H>{t("byChannel")}</H>{byChannel}</>)}
    </div>
  );
}

function QualityBody({ data }: { data: TenantQuality }) {
  const t = useT(T360);
  const columns = useMemo(() => gateColumns(t), [t]);
  const p = data.pieces;
  const e = data.edits;
  return (
    <>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 10 }}>
        <KpiTile size="sm" label={t("piecesWritten")} value={String(p.total)}
          hint={Object.entries(p.by_status).map(([s, n]) => `${n} ${s}`).join(" · ") || undefined} />
        <KpiTile size="sm" label={t("heldRate")} value={pct(p.held_rate, 1)} hint={t("heldOf", p.by_status.held ?? 0, p.shipped)} />
        <KpiTile size="sm" label={t("editRate")} value={pct(e.edit_rate, 1)}
          hint={t("editRateHint", e.tours_edited, e.tours_generated, e.tenant_edits)} />
      </div>

      <H>{t("gatesTitle", data.window_days)}</H>
      <DataTable<GateRow>
        tableId="tenant360-quality-gates" data={data.gates} columns={columns}
        getRowId={r => r.gate} searchable={false} pageSize={25} stickyHeader={false}
        emptyTitle={t("noGated")} />

      {data.held_reasons.length > 0 && (
        <>
          <H>{t("heldReasons")}</H>
          <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
            {data.held_reasons.map(r => (
              <div key={r.held_reason} style={{ display: "flex", gap: 12, padding: "5px 0", borderBottom: `1px solid ${K.line2}`, fontSize: 12 }}>
                <span style={{ color: K.body, minWidth: 0, overflowWrap: "anywhere" }}>{r.held_reason}</span>
                <span style={{ marginLeft: "auto", fontWeight: 700, color: K.amber }}>{r.count}</span>
              </div>
            ))}
          </div>
        </>
      )}
    </>
  );
}
