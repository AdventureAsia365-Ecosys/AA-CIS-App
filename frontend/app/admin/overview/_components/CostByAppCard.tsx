"use client";
// app/admin/overview/_components/CostByAppCard.tsx
// AA-858 — "Cost per app — this month": the AWS bill by App tag plus AI usage billed elsewhere, per app, with
// last month for comparison. Numbers come from the Platform cost module (AA-884, api/services/platform_cost.py)
// so this card and /admin/platform/cost always agree. "Not assigned to an app" is shown on purpose, with its
// top AWS services, because it is most of the bill while the App tags are new (since 16/10/2026).
// S243 — one presentation with /admin/platform/cost: row labels and hints come from the backend cost module, both pages
// show AWS bill + AI outside the AWS bill = Total, and the AI already on the bill is named under the table. Every
// column stays on a phone (the table scrolls in its own box, kit ScrollX).
import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { K, useIsPhone, useLang, useT } from "../../../_kit";
import type { Lang } from "../../../_kit/core/i18n";
import { aiOnBillNote, shownSum } from "../../../../lib/costWording";
import { COST_WORDING } from "../../../../lib/costWording.messages";
import { OPS } from "./ops.messages";
import { SourceHint } from "./SourceHint";
import { useSources } from "./sources";
import { fmtUsd, OpsCard, PHONE_CELL, TableWrap, TD as TD_BASE, TH as TH_BASE, UnavailableNote } from "./opsUi";
import type { CostData, CostRow, Section } from "./opsTypes";


function monthName(ym: string, lang: Lang = "en"): string {
  const d = new Date(`${ym}-01T00:00:00Z`);
  return d.toLocaleDateString(lang === "vi" ? "vi-VN" : "en-US", { month: "long", year: "numeric", timeZone: "UTC" });
}

/** The column head for last month: "September" / "tháng 9" (the month name without the year). */
function monthOnly(ym: string, lang: Lang): string {
  const d = new Date(`${ym}-01T00:00:00Z`);
  return d.toLocaleDateString(lang === "vi" ? "vi-VN" : "en-US", { month: "long", timeZone: "UTC" });
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
}

function Row({ r, max, cells }: { r: CostRow; max: number; cells: Cells }) {
  const { TD } = cells;
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
        <td style={{ ...TD, textAlign: "right" }}>{fmtUsd(shownSum(r.aws_usd))}</td>
        <td style={{ ...TD, textAlign: "right", color: r.ai_outside_aws_usd ? K.body : K.muted2 }}>
          {fmtUsd(shownSum(r.ai_outside_aws_usd))}
        </td>
        <td style={{ ...TD, textAlign: "right", color: K.ink, fontWeight: 600 }}>
          {fmtUsd(shownSum(r.aws_usd, r.ai_outside_aws_usd))}
        </td>
        <td style={{ ...TD, textAlign: "right", color: K.muted }}>{fmtUsd(r.prev_total_usd)}</td>
      </tr>
      {open &&
        r.top_services.map((s) => (
          <tr key={s.service}>
            <td style={{ ...TD, paddingLeft: 26, fontSize: 12, color: K.muted, overflowWrap: "anywhere" }}>{s.service}</td>
            <td style={{ ...TD, textAlign: "right", fontSize: 12, color: K.muted }}>{fmtUsd(s.amount_usd)}</td>
            <td style={TD} />
            <td style={TD} />
            <td style={TD} />
          </tr>
        ))}
    </>
  );
}

export function CostByAppCard({ section }: { section: Section<CostData> | undefined }) {
  const t = useT(OPS);
  const lang = useLang();
  const { COST_SRC } = useSources();
  return (
    <OpsCard
      testId="ops-cost"
      title={section && section.status === "ok" ? t("costTitleMonth", monthName(section.month, lang)) : t("costTitle")}
      subtitle={t("costSubtitle")}
      source={COST_SRC.card}
      link={{ href: "/admin/cost-ops", label: t("costOpsLink") }}
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
  const t = useT(OPS);
  const cw = useT(COST_WORDING);
  const lang = useLang();
  const { COST_SRC } = useSources();
  const phone = useIsPhone();
  const TH: React.CSSProperties = phone ? { ...TH_BASE, ...PHONE_CELL, fontSize: 11 } : TH_BASE;
  const TD: React.CSSProperties = phone ? { ...TD_BASE, ...PHONE_CELL } : TD_BASE;
  const cells: Cells = { TH, TD };
  const max = Math.max(0, ...data.rows.map((r) => r.total_usd));
  // The totals add up on screen and match the Cost page: AI outside = AI estimate − AI on the AWS bill, from cents.
  const totalsAiOutside =
    data.totals.ai_usd != null
      ? shownSum(data.totals.ai_usd, -(data.totals.ai_in_aws_usd ?? 0))
      : shownSum(data.totals.ai_outside_aws_usd);
  return (
    <>
      {!data.has_aws_data && (
        <div style={{ fontSize: 12, color: K.amber, marginBottom: 10 }}>
          {t("noAwsYet")}
        </div>
      )}
      <TableWrap testId="ops-cost-table">
        <table style={{ width: "100%", minWidth: 520, borderCollapse: "collapse" }}>
          <thead>
            <tr>
              <th style={{ ...TH, whiteSpace: "normal" }}>{t("colApp")}</th>
              <th data-num-col="" style={{ ...TH, textAlign: "right", whiteSpace: "normal" }}>
                {cw("awsBill")}
                <SourceHint source={cw("awsBillSrc")} size={11} />
              </th>
              <th data-num-col="" style={{ ...TH, textAlign: "right", whiteSpace: "normal" }}>
                {cw("aiOutside")}
                <SourceHint source={cw("aiOutsideSrc")} size={11} />
              </th>
              <th data-num-col="" style={{ ...TH, textAlign: "right", whiteSpace: "normal" }}>
                {t("colTotal")}
                <SourceHint source={COST_SRC.total} size={11} />
              </th>
              <th data-num-col="" style={{ ...TH, textAlign: "right", whiteSpace: "normal" }}>
                {monthOnly(data.prev_month, lang)}
                <SourceHint source={COST_SRC.prevMonth(monthName(data.prev_month, lang))} size={11} />
              </th>
            </tr>
          </thead>
          <tbody>
            {data.rows.map((r) => (
              <Row key={r.key} r={r} max={max} cells={cells} />
            ))}
            <tr data-testid="ops-cost-total">
              <td style={{ ...TD, fontWeight: 700, color: K.ink, borderBottom: "none" }}>{t("allApps")}</td>
              <td style={{ ...TD, textAlign: "right", fontWeight: 600, borderBottom: "none" }}>
                {fmtUsd(shownSum(data.totals.aws_usd))}
              </td>
              <td style={{ ...TD, textAlign: "right", fontWeight: 600, borderBottom: "none" }}>
                {fmtUsd(totalsAiOutside)}
              </td>
              <td style={{ ...TD, textAlign: "right", fontWeight: 700, color: K.ink, borderBottom: "none" }}>
                {fmtUsd(shownSum(data.totals.aws_usd, totalsAiOutside))}
              </td>
              <td style={{ ...TD, textAlign: "right", color: K.muted, borderBottom: "none" }}>
                {fmtUsd(data.totals.prev_total_usd)}
              </td>
            </tr>
          </tbody>
        </table>
      </TableWrap>
      <ul style={{ margin: "10px 0 0", paddingLeft: 16, fontSize: 11, color: K.muted, display: "grid", gap: 2 }}>
        {data.notes.map((n) => (
          <li key={n}>{n}</li>
        ))}
        {data.totals.ai_usd != null && (
          <li data-testid="ops-cost-ai-note">
            {aiOnBillNote(data.totals.ai_usd, data.totals.ai_in_aws_usd ?? 0, fmtUsd, COST_WORDING[lang])}
            <SourceHint source={COST_SRC.aiNote} size={11} />
          </li>
        )}
        {data.aws_covered_to && <li>{t("awsUpTo", data.aws_covered_to)}</li>}
      </ul>
    </>
  );
}
