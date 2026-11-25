"use client";
// app/admin/overview/_status/Slo.tsx — AA-1142 (P2): availability per app against its objective, the error budget
// left over the budget window and the fast / slow burn rates (SRE Workbook multiwindow alert). The objective, the
// windows, the budget-left warn line and the factors come from the API (cis.ops.status_slo_* / status_burn_* settings, editable on Platform ›
// Settings); a burn above its factor is also a Needs-attention item (SEV2 fast, SEV3 slow). A level is said in words
// and a glyph, never by colour alone; a window with too few events is grey ("few events"), never green. Kit only.

import Link from "next/link";
import { DataTable, K, useT, type ColumnDef } from "../../../_kit";
import { fmtNum, inApps, type Section, type View } from "@/lib/opsStatus";
import { budgetBar, budgetLevel, burnLevel, fmtBurn, type BurnWindow, type Slo, type SloRow } from "@/lib/opsStatusP2";
import { P2 } from "../p2.messages";
import { Card, LevelGlyph, NotMeasured, WhenUp, levelColor, small } from "./ui";
import { useWords } from "./words";

export function useP2() {
  return useT(P2);
}
type PT = ReturnType<typeof useP2>;

function burnCell(w: BurnWindow | undefined, t: PT) {
  if (!w || w.state === "none") return <span style={small}>{t("noEvents")}</span>;
  const level = burnLevel(w);
  return (
    <span style={{ display: "grid", gap: 2 }}>
      <span style={{ fontWeight: 600, color: levelColor(level) }}>
        <LevelGlyph level={level} />{fmtBurn(w.burn)}{w.state === "alert" ? ` · ${t("burnAlert")}` : ""}
      </span>
      <span style={small}>{w.state === "few" ? t("few") : t("ofEvents", w.bad, w.events)}</span>
    </span>
  );
}

function budgetCell(r: SloRow, warnPct: number, t: PT) {
  const left = r.budget_left_pct;
  const level = budgetLevel(left, warnPct);
  return (
    <span style={{ display: "grid", gap: 3, minWidth: 110 }}>
      <span role="img" aria-label={left == null ? "—" : t("budgetLeft", fmtNum(left))}
        style={{ height: 6, borderRadius: 3, background: K.line, overflow: "hidden" }}>
        <span style={{ display: "block", height: "100%", width: `${budgetBar(left)}%`,
          background: level === "crit" ? K.danger : level === "warn" ? K.amber : K.accent }} />
      </span>
      <span style={{ ...small, color: levelColor(level) }}>
        <LevelGlyph level={level} />{left == null ? "—" : left <= 0 ? t("budgetSpent") : t("budgetLeft", fmtNum(left))}
      </span>
    </span>
  );
}

function columns(s: Slo, t: PT, app: (id: string) => string): ColumnDef<SloRow>[] {
  return [
    { id: "app", header: t("colApp"), accessorFn: (r) => app(r.app),
      cell: ({ row }) => <b style={{ color: K.ink }}>{app(row.original.app)}</b> },
    { id: "avail", header: t("colAvailability"), accessorFn: (r) => r.availability_pct ?? null,
      cell: ({ row }) => {
        const r = row.original;
        if (!r.measured) return <NotMeasured reason={r.reason === "no_health_samples" ? t("rNoSamples")
          : t("rCloudwatch")} />;
        const lv = r.below_target ? "crit" : "ok";
        return (
          <span style={{ fontWeight: 600, color: levelColor(lv) }}>
            <LevelGlyph level={lv} />{r.availability_pct == null ? "—" : `${fmtNum(r.availability_pct, 3)} %`}
            <span style={small}>{` / ${r.target_pct} %`}{r.below_target ? ` · ${t("belowTarget")}` : ""}</span>
          </span>
        );
      } },
    { id: "budget", header: t("colBudget"), accessorFn: (r) => r.budget_left_pct ?? null,
      cell: ({ row }) => (row.original.measured ? budgetCell(row.original, s.rules.budget_warn_pct, t) : "—") },
    { id: "fast", header: t("colFast", s.rules.fast.window_min), accessorFn: (r) => r.fast?.burn ?? null,
      cell: ({ row }) => (row.original.measured ? burnCell(row.original.fast, t) : "—") },
    { id: "slow", header: t("colSlow", s.rules.slow.window_hours), accessorFn: (r) => r.slow?.burn ?? null,
      cell: ({ row }) => (row.original.measured ? burnCell(row.original.slow, t) : "—") },
    { id: "basis", header: t("colBasis"), enableSorting: false,
      cell: ({ row }) => <span style={small}>{row.original.basis === "health samples" ? t("basisSamples")
        : row.original.basis ? t("basisRequests") : "—"}</span> },
  ];
}

export function SloCard({ section, view }: { section: Section<Slo> | undefined; view: View }) {
  const t = useP2();
  const w = useWords();
  if (!section) return null;
  return (
    <Card title={t("sloTitle")} testId="status-slo" asOf={section.as_of} refreshS={section.refresh_s}
      actions={<Link href="/admin/platform/settings?app=cis&area=ops" style={{ ...small, color: K.accentDeep,
        fontWeight: 600 }}>{t("lSettings")}</Link>}>
      <WhenUp section={section} testId="status-slo-down">
        {(s) => (
          <>
            <span style={small}>
              {t("sloNote", s.rules.target_pct, s.rules.window_days, s.rules.fast.window_min, s.rules.fast.factor,
                s.rules.slow.window_hours, s.rules.slow.factor)}{" "}{t("sloBudgetWarn", s.rules.budget_warn_pct)}{" "}
              {t("sloFew", s.rules.min_requests)}
            </span>
            <DataTable<SloRow> tableId="status-slo" data={s.rows.filter((r) => inApps(r.app, view.apps))}
              columns={columns(s, t, w.app)} getRowId={(r) => r.app} searchable={false} paginate={false} compact
              tableMinWidth={720} />
          </>
        )}
      </WhenUp>
    </Card>
  );
}
