// app/admin/cost-ops/costOpsUi.tsx — AA-665: small kit-styled pieces shared by the Cost Operations panels that are
// built on the kit (Forecast, DataForSEO runs, Jev). The older tabs of page.tsx still use the legacy adminUi cards.

import type React from "react";
import { K, RADIUS, sans, type Tone } from "../../_kit";

export const panelStyle: React.CSSProperties = {
  background: K.card, border: `1px solid ${K.line}`, borderRadius: RADIUS.lg, padding: "14px 16px",
  fontFamily: sans, fontSize: 13, color: K.ink, minWidth: 0, marginBottom: 20,
};

export const noteStyle: React.CSSProperties = { fontSize: 12, color: K.muted, fontFamily: sans, lineHeight: 1.5 };

export const numStyle: React.CSSProperties = { fontVariantNumeric: "tabular-nums", whiteSpace: "nowrap" };

export function PanelTitle({ children, aside }: { children: React.ReactNode; aside?: React.ReactNode }) {
  return (
    <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", gap: 12, flexWrap: "wrap",
                  marginBottom: 10 }}>
      <h2 style={{ fontSize: 14, fontWeight: 600, margin: 0, color: K.ink }}>{children}</h2>
      {aside}
    </div>
  );
}

export function Stat({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: Tone }) {
  const colour = tone === "danger" ? K.danger : tone === "warning" ? K.amber : tone === "success" ? K.success : K.ink;
  return (
    <div style={{ ...panelStyle, marginBottom: 0, padding: "10px 12px", display: "flex", flexDirection: "column",
                  gap: 2 }}>
      <span style={{ fontSize: 12, color: K.muted }}>{label}</span>
      <span style={{ ...numStyle, fontSize: 18, fontWeight: 600, color: colour }}>{value}</span>
      {sub && <span style={{ fontSize: 11.5, color: K.muted2 }}>{sub}</span>}
    </div>
  );
}

export const statGrid: React.CSSProperties = {
  display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 170px), 1fr))", gap: 12, marginBottom: 16,
};

export function usd(n: number | null | undefined, digits = 2): string {
  if (n == null || Number.isNaN(n)) return "—";
  if (n > 0 && n < 0.01 && digits <= 2) return `$${n.toFixed(4)}`;
  return `$${n.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
}

export function int(n: number | null | undefined): string {
  return n == null ? "—" : n.toLocaleString("en-US");
}

export type BudgetStatus = "over" | "warn" | "ok" | "no_budget" | "no_data";

export const STATUS_META: Record<BudgetStatus, { tone: Tone; label: string }> = {
  over: { tone: "danger", label: "Over budget" },
  warn: { tone: "warning", label: "Near budget" },
  ok: { tone: "success", label: "Within budget" },
  no_budget: { tone: "neutral", label: "No budget" },
  no_data: { tone: "neutral", label: "No data" },
};
