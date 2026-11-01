"use client";
// app/admin/overview/_components/CostByAppCard.tsx
// AA-858 — "Cost per app — this month": the AWS bill by App tag plus AI usage billed elsewhere, per app, with
// last month for comparison. Numbers come from the Platform cost module (AA-884, api/services/platform_cost.py)
// so this card and /admin/platform/cost always agree. "Not assigned to an app" is shown on purpose, with its
// top AWS services, because it is most of the bill while the App tags are new (since 16/10/2026).
// S243 — one presentation with /admin/platform/cost: row labels and hints come from the backend cost module, both pages
// show AWS bill + AI outside the AWS bill = Total, and the AI already on the bill is named under the table. Every
// column stays on a phone (the kit DataTable: a card per line on a phone, AA-1006).
import { useState } from "react";
import type { ColumnDef } from "@tanstack/react-table";
import { ChevronDown, ChevronRight } from "lucide-react";
import { DataTable, K, LinkButton, useLang, useT } from "../../../_kit";
import type { Lang } from "../../../_kit/core/i18n";
import { aiOnBillNote, shownSum } from "../../../../lib/costWording";
import { COST_WORDING } from "../../../../lib/costWording.messages";
import { OPS } from "./ops.messages";
import { SourceHint } from "./SourceHint";
import { useSources } from "./sources";
import { fmtUsd, hintColumn, OpsCard, UnavailableNote } from "./opsUi";
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

/** One line of the cost table: an app row, one of its top AWS services (shown when the row is open) or the total. */
type CostLine =
  | { id: string; kind: "row"; r: CostRow }
  | { id: string; kind: "service"; service: string; amount: number | null }
  | { id: string; kind: "total" };

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
  const [open, setOpen] = useState<ReadonlySet<string>>(new Set());
  const toggle = (key: string) =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  const max = Math.max(0, ...data.rows.map((r) => r.total_usd));
  // The totals add up on screen and match the Cost page: AI outside = AI estimate − AI on the AWS bill, from cents.
  const totalsAiOutside =
    data.totals.ai_usd != null
      ? shownSum(data.totals.ai_usd, -(data.totals.ai_in_aws_usd ?? 0))
      : shownSum(data.totals.ai_outside_aws_usd);

  const lines: CostLine[] = [];
  for (const r of data.rows) {
    lines.push({ id: `row:${r.key}`, kind: "row", r });
    if (open.has(r.key)) {
      for (const s of r.top_services) lines.push({ id: `svc:${r.key}:${s.service}`, kind: "service", service: s.service, amount: s.amount_usd });
    }
  }
  lines.push({ id: "total", kind: "total" });

  const money = (v: number | null | undefined, style: React.CSSProperties = {}) => (
    <span style={{ fontVariantNumeric: "tabular-nums", ...style }}>{fmtUsd(v ?? null)}</span>
  );
  const columns: ColumnDef<CostLine, unknown>[] = [
    {
      id: t("colApp"),
      header: t("colApp"),
      enableSorting: false,
      cell: ({ row: { original: l } }) => {
        if (l.kind === "total") {
          return <span data-testid="ops-cost-total" style={{ fontWeight: 700, color: K.ink }}>{t("allApps")}</span>;
        }
        if (l.kind === "service") {
          return <span style={{ paddingLeft: 16, fontSize: 12, color: K.muted, overflowWrap: "anywhere" }}>{l.service}</span>;
        }
        const r = l.r;
        const isOpen = open.has(r.key);
        return (
          <span data-testid={`ops-cost-row-${r.key}`} style={{ display: "block", color: K.ink, fontWeight: r.kind === "app" ? 600 : 500 }}>
            <span style={{ display: "inline-flex", alignItems: "center", gap: 4, flexWrap: "wrap" }}>
              {r.top_services.length > 0 ? (
                <LinkButton tone="inherit" size="inherit" aria-expanded={isOpen} onClick={() => toggle(r.key)}
                  style={{ gap: 2, color: r.kind === "untagged" ? K.amber : K.ink, fontWeight: "inherit" }}>
                  {isOpen ? <ChevronDown size={13} aria-hidden /> : <ChevronRight size={13} aria-hidden />}
                  {r.label}
                </LinkButton>
              ) : (
                r.label
              )}
              {r.hint && <SourceHint source={r.hint} size={11} />}
            </span>
            <ShareBar value={r.total_usd} max={max} />
          </span>
        );
      },
    },
    {
      ...hintColumn(cw("awsBill"), cw("awsBillSrc")),
      cell: ({ row: { original: l } }) =>
        l.kind === "row" ? money(shownSum(l.r.aws_usd))
          : l.kind === "service" ? money(l.amount, { fontSize: 12, color: K.muted })
            : money(shownSum(data.totals.aws_usd), { fontWeight: 600 }),
    },
    {
      ...hintColumn(cw("aiOutside"), cw("aiOutsideSrc")),
      cell: ({ row: { original: l } }) =>
        l.kind === "row" ? money(shownSum(l.r.ai_outside_aws_usd), { color: l.r.ai_outside_aws_usd ? K.body : K.muted2 })
          : l.kind === "total" ? money(totalsAiOutside, { fontWeight: 600 }) : null,
    },
    {
      ...hintColumn(t("colTotal"), COST_SRC.total),
      cell: ({ row: { original: l } }) =>
        l.kind === "row" ? money(shownSum(l.r.aws_usd, l.r.ai_outside_aws_usd), { color: K.ink, fontWeight: 600 })
          : l.kind === "total" ? money(shownSum(data.totals.aws_usd, totalsAiOutside), { color: K.ink, fontWeight: 700 })
            : null,
    },
    {
      ...hintColumn(monthOnly(data.prev_month, lang), COST_SRC.prevMonth(monthName(data.prev_month, lang))),
      cell: ({ row: { original: l } }) =>
        l.kind === "row" ? money(l.r.prev_total_usd, { color: K.muted })
          : l.kind === "total" ? money(data.totals.prev_total_usd, { color: K.muted }) : null,
    },
  ];
  return (
    <>
      {!data.has_aws_data && (
        <div style={{ fontSize: 12, color: K.amber, marginBottom: 10 }}>
          {t("noAwsYet")}
        </div>
      )}
      <div data-testid="ops-cost-table">
        <DataTable<CostLine>
          tableId="ops-cost-by-app"
          data={lines}
          columns={columns}
          getRowId={(l) => l.id}
          searchable={false}
          paginate={false}
          compact
          tableMinWidth={520}
        />
      </div>
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
