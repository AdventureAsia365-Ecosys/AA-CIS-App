"use client";
// app/admin/overview/_components/CostByAppCard.tsx
// AA-858 — "Cost per app — this month": the AWS bill by App tag plus AI usage billed elsewhere, per app, with
// last month for comparison. Numbers come from the Platform cost module (AA-884, api/services/platform_cost.py)
// so this card and /admin/platform/cost always agree. "Not assigned to an app" is shown on purpose, with its
// top AWS services, because it is most of the bill while the App tags are new (since 16/10/2026).
import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { K, useIsPhone } from "../../../_kit";
import { SourceHint } from "./SourceHint";
import { fmtUsd, OpsCard, PHONE_CELL, TableWrap, TD as TD_BASE, TH as TH_BASE, UnavailableNote } from "./opsUi";
import type { CostData, CostRow, Section } from "./opsTypes";

const AWS_SRC = "AWS Cost Explorer, account acc2, cost by the App tag (stored daily by the Platform cost module)";
const AI_SRC =
  "LLM gateway estimates (shared.llm_call_log / llm_call_daily) for AI billed outside this AWS account: " +
  "other Bedrock accounts, OpenAI, Jev";

function monthName(ym: string): string {
  const d = new Date(`${ym}-01T00:00:00Z`);
  return d.toLocaleDateString("en-US", { month: "long", year: "numeric", timeZone: "UTC" });
}

function ShareBar({ value, max }: { value: number; max: number }) {
  const pct = max > 0 ? Math.max(2, Math.round((100 * value) / max)) : 0;
  return (
    <div style={{ height: 4, background: K.line2, borderRadius: 2, marginTop: 4 }} aria-hidden>
      <div style={{ width: `${pct}%`, height: "100%", background: K.accent, borderRadius: 2 }} />
    </div>
  );
}

interface Cells {
  TH: React.CSSProperties;
  TD: React.CSSProperties;
  phone: boolean;
}

function Row({ r, max, cells }: { r: CostRow; max: number; cells: Cells }) {
  const { TD, phone } = cells;
  const [open, setOpen] = useState(false);
  const canOpen = r.top_services.length > 0;
  const isUntagged = r.kind === "untagged";
  return (
    <>
      <tr data-testid={`ops-cost-row-${r.key}`}>
        <td style={{ ...TD, color: K.ink, fontWeight: r.kind === "app" ? 600 : 500 }}>
          <span style={{ display: "inline-flex", alignItems: "center", gap: 4, flexWrap: "wrap" }}>
            {canOpen ? (
              <button
                type="button"
                onClick={() => setOpen((v) => !v)}
                aria-expanded={open}
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 2,
                  padding: 0,
                  border: "none",
                  background: "transparent",
                  color: isUntagged ? K.amber : K.ink,
                  font: "inherit",
                  cursor: "pointer",
                  textAlign: "left",
                }}
              >
                {open ? <ChevronDown size={13} aria-hidden /> : <ChevronRight size={13} aria-hidden />}
                {r.label}
              </button>
            ) : (
              r.label
            )}
            {r.hint && <SourceHint source={r.hint} size={11} />}
          </span>
          <ShareBar value={r.total_usd} max={max} />
        </td>
        <td style={{ ...TD, textAlign: "right" }}>{fmtUsd(r.aws_usd)}</td>
        <td style={{ ...TD, textAlign: "right", color: r.ai_outside_aws_usd ? K.body : K.muted2 }}>
          {fmtUsd(r.ai_outside_aws_usd)}
        </td>
        <td style={{ ...TD, textAlign: "right", color: K.ink, fontWeight: 600 }}>{fmtUsd(r.total_usd)}</td>
        {!phone && <td style={{ ...TD, textAlign: "right", color: K.muted }}>{fmtUsd(r.prev_total_usd)}</td>}
      </tr>
      {open &&
        r.top_services.map((s) => (
          <tr key={s.service}>
            <td style={{ ...TD, paddingLeft: 26, fontSize: 12, color: K.muted, overflowWrap: "anywhere" }}>{s.service}</td>
            <td style={{ ...TD, textAlign: "right", fontSize: 12, color: K.muted }}>{fmtUsd(s.amount_usd)}</td>
            <td style={TD} />
            <td style={TD} />
            {!phone && <td style={TD} />}
          </tr>
        ))}
    </>
  );
}

export function CostByAppCard({ section, source }: { section: Section<CostData> | undefined; source: string }) {
  return (
    <OpsCard
      testId="ops-cost"
      title={section && section.status === "ok" ? `Cost per app — ${monthName(section.month)} so far` : "Cost per app"}
      subtitle="What each app costs to run this month, with last month for comparison (US dollars)"
      source={source}
      link={{ href: "/admin/llm-usage", label: "External Spend detail" }}
    >
      {!section ? null : section.status !== "ok" ? (
        <UnavailableNote section={section} />
      ) : (
        <CostBody data={section} />
      )}
    </OpsCard>
  );
}

function CostBody({ data }: { data: CostData }) {
  const phone = useIsPhone();
  const TH: React.CSSProperties = phone ? { ...TH_BASE, ...PHONE_CELL, fontSize: 11 } : TH_BASE;
  const TD: React.CSSProperties = phone ? { ...TD_BASE, ...PHONE_CELL } : TD_BASE;
  const cells: Cells = { TH, TD, phone };
  const max = Math.max(0, ...data.rows.map((r) => r.total_usd));
  return (
    <>
      {!data.has_aws_data && (
        <div style={{ fontSize: 12, color: K.amber, marginBottom: 10 }}>
          The AWS bill for this month has not been fetched yet; only AI usage is shown.
        </div>
      )}
      <TableWrap testId="ops-cost-table">
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead>
            <tr>
              <th style={{ ...TH, whiteSpace: "normal" }}>App</th>
              <th style={{ ...TH, textAlign: "right", whiteSpace: "normal" }}>
                AWS bill
                <SourceHint source={AWS_SRC} size={11} />
              </th>
              <th style={{ ...TH, textAlign: "right", whiteSpace: "normal" }}>
                AI elsewhere
                <SourceHint source={AI_SRC} size={11} />
              </th>
              <th style={{ ...TH, textAlign: "right", whiteSpace: "normal" }}>
                Total
                <SourceHint source="AWS bill + AI elsewhere (AI already on this AWS bill is not counted twice)" size={11} />
              </th>
              {!phone && (
                <th style={{ ...TH, textAlign: "right", whiteSpace: "normal" }}>
                  {monthName(data.prev_month).split(" ")[0]}
                  <SourceHint source={`Same sources, whole of ${monthName(data.prev_month)}`} size={11} />
                </th>
              )}
            </tr>
          </thead>
          <tbody>
            {data.rows.map((r) => (
              <Row key={r.key} r={r} max={max} cells={cells} />
            ))}
            <tr data-testid="ops-cost-total">
              <td style={{ ...TD, fontWeight: 700, color: K.ink, borderBottom: "none" }}>All apps</td>
              <td style={{ ...TD, textAlign: "right", fontWeight: 600, borderBottom: "none" }}>{fmtUsd(data.totals.aws_usd)}</td>
              <td style={{ ...TD, textAlign: "right", fontWeight: 600, borderBottom: "none" }}>
                {fmtUsd(data.totals.ai_outside_aws_usd)}
              </td>
              <td style={{ ...TD, textAlign: "right", fontWeight: 700, color: K.ink, borderBottom: "none" }}>
                {fmtUsd(data.totals.total_usd)}
              </td>
              {!phone && (
                <td style={{ ...TD, textAlign: "right", color: K.muted, borderBottom: "none" }}>
                  {fmtUsd(data.totals.prev_total_usd)}
                </td>
              )}
            </tr>
          </tbody>
        </table>
      </TableWrap>
      <ul style={{ margin: "10px 0 0", paddingLeft: 16, fontSize: 11, color: K.muted, display: "grid", gap: 2 }}>
        {data.notes.map((n) => (
          <li key={n}>{n}</li>
        ))}
        {data.aws_covered_to && <li>AWS numbers include days up to {data.aws_covered_to}.</li>}
        {phone && data.totals.prev_total_usd != null && (
          <li>
            {monthName(data.prev_month)}, all apps: {fmtUsd(data.totals.prev_total_usd)} (per app on a wider screen).
          </li>
        )}
      </ul>
    </>
  );
}
