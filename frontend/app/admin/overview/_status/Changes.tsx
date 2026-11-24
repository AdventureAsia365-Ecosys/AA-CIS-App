"use client";
// app/admin/overview/_status/Changes.tsx — AA-1141 (P1 feature 1): the "What changed" panel: every deploy, setting
// change, migration, wave and route control change of the range (api/services/ops_status/changes.py), filtered by the
// page's app filter and by type (chips kept in the URL as `chg`), each linking the commit, the setting history, the
// data model, the jobs or the routing page. The same changes are drawn on every chart (Signals.tsx); a type is told
// by its letter and line pattern, never by colour alone. Kit only.

import Link from "next/link";
import { Badge, DataTable, K, useFormat, type ColumnDef } from "../../../_kit";
import {
  CHANGE_TYPES, countBy, filterChanges, inApps, sortingParam, tableSorting, withSort, type ChangeRow, type Changes,
  type Section, type View, type SetView,
} from "@/lib/opsStatus";
import { changePhrase } from "@/lib/opsStatusText";
import { Card, ChipFilter, WhenUp, rowWrap, small, useST, type ST } from "./ui";
import { useWords } from "./words";

/** The letter of a change type (the chart marker label and the table badge). */
export const CHANGE_LETTER: Readonly<Record<string, string>> = {
  deploy: "D", setting: "S", migration: "M", wave: "W", route: "R",
};
/** Line pattern per type: the charts tell types apart without colour (WCAG 1.4.1). */
export const CHANGE_DASH: Readonly<Record<string, string>> = {
  deploy: "2 3", setting: "6 3", migration: "1 2", wave: "8 2 2 2", route: "4 2 1 2",
};

export function typeLabel(type: string, t: ST): string {
  return t(`chg_${type}` as Parameters<ST>[0]) as string;
}

function ChangeText({ r }: { r: ChangeRow }) {
  const t = useST();
  const p = changePhrase(r.msg);
  const text = p ? (t as unknown as (k: string, ...a: string[]) => string)(p.key, ...p.args) : r.title;
  const external = r.href?.startsWith("http");
  return (
    <span style={{ display: "grid", gap: 2, minWidth: 0, overflowWrap: "anywhere" }}>
      {r.href ? (
        external
          ? <a href={r.href} target="_blank" rel="noreferrer" style={{ color: K.accentDeep, fontWeight: 600 }}>{text}</a>
          : <Link href={r.href} style={{ color: K.accentDeep, fontWeight: 600 }}>{text}</Link>
      ) : text}
      {r.type === "migration" && r.title !== text && <span style={small}>{r.title}</span>}
    </span>
  );
}

function columns(t: ST, app: (id: string) => string, formatDateTime: (d: string | null) => string):
  ColumnDef<ChangeRow>[] {
  return [
    { id: "t", header: t("colTime"), accessorFn: (r) => r.t, cell: ({ row }) => formatDateTime(row.original.t) },
    { id: "type", header: t("chgType"), accessorFn: (r) => r.type,
      cell: ({ row }) => <Badge tone="neutral" dot={false}>{CHANGE_LETTER[row.original.type] ?? "?"} · {typeLabel(
        row.original.type, t)}</Badge> },
    { id: "app", header: t("colApp"), accessorFn: (r) => app(r.app) },
    { id: "change", header: t("colChange"), accessorFn: (r) => r.title, cell: ({ row }) => <ChangeText r={row.original} /> },
    { id: "by", header: t("colBy"), accessorFn: (r) => r.by ?? "", cell: ({ row }) => row.original.by ?? "—" },
  ];
}

export function ChangesCard({ section, view, setView }: {
  section: Section<Changes> | undefined; view: View; setView: SetView;
}) {
  const t = useST();
  const w = useWords();
  const { formatDateTime } = useFormat();
  return (
    <Card title={t("changesTitle")} testId="status-changes" asOf={section?.as_of} refreshS={section?.refresh_s}>
      <span style={small}>{t("changesNote")}</span>
      <WhenUp section={section}>
        {(c) => {
          const scoped = c.rows.filter((r) => inApps(r.app, view.apps));
          const counts = countBy(scoped, (r) => r.type);
          const rows = filterChanges(c.rows, view);
          const missing = Object.keys(c.unavailable ?? {});
          return (
            <>
              <div style={rowWrap}>
                <ChipFilter label={t("chgType")} testId="status-chg-filter" value={view.chg}
                  onChange={(f) => setView((v) => ({ chg: f(v.chg) }))}
                  options={CHANGE_TYPES.map((k) => ({ value: k, label: `${CHANGE_LETTER[k]} · ${typeLabel(k, t)}`,
                    count: counts[k] ?? 0 }))} />
              </div>
              {missing.length > 0 && <span style={small}>{t("chgUnavailable", missing.map((k) => typeLabel(k, t))
                .join(", "))}</span>}
              <DataTable<ChangeRow> tableId="status-changes" data={rows} columns={columns(t, w.app, formatDateTime)}
                getRowId={(r) => r.id} searchable={false} compact pageSize={25} tableMinWidth={640}
                emptyTitle={t("chgNone")} sorting={tableSorting(view.sort.chg)}
                onSortingChange={(s) => setView((v) => ({ sort: withSort(v.sort, "chg", sortingParam(s)) }))} />
            </>
          );
        }}
      </WhenUp>
    </Card>
  );
}
