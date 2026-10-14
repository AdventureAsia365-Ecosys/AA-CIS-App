"use client";
// app/admin/tenants/_components/Tenant360Quality.tsx
// AA-766 — Tenant 360 Quality tab: T10 gate verdicts per gate (pass / blocking fail / warn / held
// pieces), the piece status mix, held reasons and the tenant edit rate on T2 tour versions. From
// GET /admin/tenants/{id}/quality. The per-channel gate telemetry AA-666 showed stays below it.
// Admin-only: the tenant sees content, never these verdicts (AA-613).

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { A, mono, serif } from "../../_components/adminUi";
import { DataTable, ErrorState, Spinner, apiGet } from "../../../_kit";
import { type TenantQuality, pct } from "./tenant360";

const WINDOWS = [7, 30, 90] as const;
type GateRow = TenantQuality["gates"][number];
const tabular: React.CSSProperties = { fontVariantNumeric: "tabular-nums" };
function count(color: string) {
  return function CountCell(c: { getValue: () => unknown }) {
    const n = Number(c.getValue());
    return <span style={{ ...tabular, color: n > 0 ? color : A.muted }}>{n}</span>;
  };
}

const GATE_COLUMNS: ColumnDef<GateRow, unknown>[] = [
  { accessorKey: "gate", header: "Gate", cell: c => <span style={{ fontFamily: mono, fontSize: 11.5 }}>{String(c.getValue())}</span> },
  { accessorKey: "evaluated", header: "Evaluated", cell: c => <span style={tabular}>{Number(c.getValue())}</span> },
  { accessorKey: "passed", header: "Passed", cell: count(A.green) },
  { accessorKey: "pass_rate", header: "Pass rate", cell: c => <strong style={tabular}>{pct(c.getValue() as number | null)}</strong> },
  { accessorKey: "failed_blocking", header: "Blocking fail", cell: count(A.red) },
  { accessorKey: "failed_warn", header: "Warn", cell: count(A.amber) },
  { accessorKey: "held_pieces", header: "Held pieces", cell: count(A.amber) },
];

function Kpi({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div style={{ padding: "10px 14px", background: A.card, border: `1px solid ${A.line}`, borderRadius: 8, minWidth: 0 }}>
      <div style={{ fontSize: 10, color: A.muted2, marginBottom: 3 }}>{label}</div>
      <div style={{ fontFamily: serif, fontSize: 18, fontWeight: 500, color: A.ink, fontVariantNumeric: "tabular-nums" }}>{value}</div>
      {hint && <div style={{ fontSize: 10.5, color: A.muted2, marginTop: 2 }}>{hint}</div>}
    </div>
  );
}

function H({ children }: { children: React.ReactNode }) {
  return <div style={{ fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: A.muted, margin: "18px 0 8px" }}>{children}</div>;
}

export function Tenant360Quality({ tenantId, byChannel }: { tenantId: string; byChannel?: React.ReactNode }) {
  const [days, setDays] = useState<number>(30);
  const q = useQuery({
    queryKey: ["tenant360", tenantId, "quality", days],
    queryFn: () => apiGet<TenantQuality>(`/api/admin/tenants/${tenantId}/quality?days=${days}`, { admin: true }),
  });

  return (
    <div style={{ minWidth: 0 }}>
      <div style={{ display: "flex", gap: 6, marginBottom: 12, alignItems: "center", flexWrap: "wrap" }}>
        <span style={{ fontSize: 11.5, color: A.muted }}>Window</span>
        {WINDOWS.map(d => (
          <button key={d} type="button" aria-pressed={days === d} onClick={() => setDays(d)}
            style={{ padding: "4px 11px", borderRadius: 20, fontSize: 12, cursor: "pointer",
              border: `1px solid ${days === d ? A.accent : A.line}`, background: days === d ? A.accentTint : A.bg,
              color: days === d ? A.accentDeep : A.muted, fontWeight: days === d ? 700 : 400 }}>{d} days</button>
        ))}
      </div>

      {q.isLoading ? (
        <div style={{ fontSize: 12, color: A.muted, display: "flex", gap: 6, alignItems: "center" }}><Spinner size={12} /> Loading quality…</div>
      ) : q.isError || !q.data ? (
        <ErrorState message={`Failed to load quality: ${(q.error as Error)?.message ?? "unknown error"}`} onRetry={() => q.refetch()} />
      ) : (
        <QualityBody data={q.data} />
      )}

      {byChannel && (<><H>Gate telemetry by channel (all time)</H>{byChannel}</>)}
    </div>
  );
}

function QualityBody({ data }: { data: TenantQuality }) {
  const p = data.pieces;
  const e = data.edits;
  return (
    <>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 10 }}>
        <Kpi label="Pieces written" value={String(p.total)}
          hint={Object.entries(p.by_status).map(([s, n]) => `${n} ${s}`).join(" · ") || undefined} />
        <Kpi label="Held rate" value={pct(p.held_rate, 1)} hint={`${p.by_status.held ?? 0} held of ${p.shipped} shipped`} />
        <Kpi label="Tenant edit rate (T2 tours)" value={pct(e.edit_rate, 1)}
          hint={`${e.tours_edited} of ${e.tours_generated} rewritten tours edited · ${e.tenant_edits} edit version${e.tenant_edits === 1 ? "" : "s"}`} />
      </div>

      <H>T10 gates — last {data.window_days} days</H>
      <DataTable<GateRow>
        tableId="tenant360-quality-gates" data={data.gates} columns={GATE_COLUMNS}
        getRowId={r => r.gate} searchable={false} pageSize={25} stickyHeader={false}
        emptyTitle="No gated pieces in this window" />

      {data.held_reasons.length > 0 && (
        <>
          <H>Held reasons</H>
          <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
            {data.held_reasons.map(r => (
              <div key={r.held_reason} style={{ display: "flex", gap: 12, padding: "5px 0", borderBottom: `1px solid ${A.line2}`, fontSize: 12 }}>
                <span style={{ color: A.body, minWidth: 0, overflowWrap: "anywhere" }}>{r.held_reason}</span>
                <span style={{ marginLeft: "auto", fontWeight: 700, color: A.amber }}>{r.count}</span>
              </div>
            ))}
          </div>
        </>
      )}
    </>
  );
}
