"use client";
// app/admin/platform/settings/SettingsTable.tsx — the settings of one app on Platform › Settings: the search box, the
// area tabs and the table.
//
// AA-1096 (S280, tech lead: "the search only filters the selected area and does not match the key"): the search box
// sits above the area tabs and searches every area of the app by key, label and description (lib searchSettings);
// while a query is typed the table shows the matches of all areas, each tagged with its area, and clearing it returns
// to the area tab. The box is wide on a desktop (a long key fits) and full width on a phone.

import { Search } from "lucide-react";
import { useMemo, useState } from "react";
import { Badge, DataTable, Input, K, Tabs, useT, type ColumnDef } from "../../../_kit";
import { groupByArea, searchSettings, type SettingApp, type SettingItem } from "@/lib/settingsRegistry";
import { SETTINGS } from "./messages";

const small = { fontSize: 12, color: K.muted, lineHeight: 1.45 } as const;

function SearchBox({ value, onChange }: { value: string; onChange: (q: string) => void }) {
  const t = useT(SETTINGS);
  return (
    <div style={{ position: "relative", width: "100%", maxWidth: 560 }}>
      <Search size={14} aria-hidden style={{ position: "absolute", left: 10, top: "50%", transform: "translateY(-50%)",
        color: K.muted, pointerEvents: "none" }} />
      <Input type="search" value={value} onChange={(e) => onChange(e.target.value)} aria-label={t("searchLabel")}
        placeholder={t("searchPlaceholder")} style={{ width: "100%", paddingLeft: 30 }}
        data-testid="settings-search" />
    </div>
  );
}

export function SettingsTable({ app, settings, initialArea, columns }: {
  app: SettingApp; settings: SettingItem[]; initialArea: string; columns: ColumnDef<SettingItem, unknown>[];
}) {
  const t = useT(SETTINGS);
  const groups = useMemo(() => groupByArea(settings), [settings]);
  const [area, setArea] = useState(initialArea);
  const [query, setQuery] = useState("");
  const found = useMemo(() => searchSettings(settings, query), [settings, query]);
  const searching = query.trim() !== "";
  const cols = useMemo<ColumnDef<SettingItem, unknown>[]>(() => searching ? [
    { id: "area", header: t("colArea"), accessorKey: "area",
      cell: ({ getValue }) => <Badge tone="neutral" dot={false}>{String(getValue())}</Badge> },
    ...columns,
  ] : columns, [searching, columns, t]);
  if (groups.length === 0) return null;
  const activeArea = groups.some((g) => g.area === area) ? area : groups[0].area;
  const rows = searching ? found : groups.find((g) => g.area === activeArea)?.items ?? [];
  return (
    <>
      <SearchBox value={query} onChange={setQuery} />
      {searching
        ? <div style={small} aria-live="polite" data-testid="settings-search-count">
          {found.length ? t("searchResults", found.length, query.trim()) : t("searchNone", query.trim())}</div>
        : <Tabs variant="underline" ariaLabel={t("title")} active={activeArea} onChange={setArea}
          tabs={groups.map((g) => ({ key: g.area, label: g.area, count: g.items.length }))} testId="settings-areas" />}
      {rows.length > 0 && (
        <DataTable<SettingItem> tableId={`settings-${app}-${searching ? "search" : activeArea}`} data={rows}
          columns={cols} getRowId={(s) => s.key} searchable={false} paginate={false} tableMinWidth={980} />
      )}
    </>
  );
}
