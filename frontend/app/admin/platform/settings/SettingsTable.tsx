"use client";
// app/admin/platform/settings/SettingsTable.tsx — the settings of one app on Platform › Settings: the search box, the
// area tabs and the table.
//
// AA-1096 (S280, tech lead: "the search only filters the selected area and does not match the key"): the search box
// sits above the area tabs and searches every area of the app by key, label and description (lib searchSettings);
// while a query is typed the table shows the matches of all areas, each tagged with its area, and clearing it returns
// to the area tab. The box is wide on a desktop (a long key fits) and full width on a phone.
// S280 (lane cis-other): `initialQuery` fills the box when the page opens (`?key=<setting key>`, e.g. the link from
// Catalogue settings › Pipeline gates to the A1 pass line).
// S280 UX fix round: the area tabs and the Area column show the area's name in the page's language (messages.ts
// areaName), and the area tab list has its own accessible name ("Areas"), apart from the app tabs.
// AA-1096 (S280, tech lead: "do the groups disappear while I search?"): while a query is typed a row of area chips
// replaces the area tabs: "All N" first (picked by default), then one chip per area with matches and its count
// (lib searchAreaCounts); a chip shows only that area's matches (lib matchesInArea). Clearing the query returns to the
// area tabs, and the next search starts again on "All".

import { Search } from "lucide-react";
import { useMemo, useState } from "react";
import {
  Badge, ButtonGroup, ChipButton, DataTable, Input, K, Tabs, useT, type ColumnDef,
} from "../../../_kit";
import {
  groupByArea, matchesInArea, searchAreaCounts, searchAreaPicked, searchSettings, type SettingApp, type SettingItem,
} from "@/lib/settingsRegistry";
import { SETTINGS } from "./messages";
import { useSettingWords } from "./useSettingWords";

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

/** Under the search box while a query is typed: the match count, then the area chips ("All N", then each area with
 *  matches and its count). */
function SearchAreas({ query, found, picked, onPick, areaName }: {
  query: string; found: SettingItem[]; picked: string | null; onPick: (area: string | null) => void;
  areaName: (area: string) => string;
}) {
  const t = useT(SETTINGS);
  const q = query.trim();
  const summary = (
    <div style={small} aria-live="polite" data-testid="settings-search-count">
      {found.length ? t("searchResults", found.length, q) : t("searchNone", q)}</div>
  );
  if (found.length === 0) return summary;
  const count = (n: number) => <span style={{ fontVariantNumeric: "tabular-nums", opacity: 0.85 }}>{n}</span>;
  return (
    <>
      {summary}
      <ButtonGroup label={t("searchAreasLabel")} testId="settings-search-areas">
        <ChipButton size="sm" selected={picked === null} onClick={() => onPick(null)} testId="settings-search-area-all">
          {t("searchAll")} {count(found.length)}
        </ChipButton>
        {searchAreaCounts(found).map(({ area, count: n }) => (
          <ChipButton key={area} size="sm" selected={picked === area} onClick={() => onPick(area)}
            testId={`settings-search-area-${area}`}>
            {areaName(area)} {count(n)}
          </ChipButton>
        ))}
      </ButtonGroup>
    </>
  );
}

export function SettingsTable({ app, settings, initialArea, initialQuery, columns }: {
  app: SettingApp; settings: SettingItem[]; initialArea: string; initialQuery?: string;
  columns: ColumnDef<SettingItem, unknown>[];
}) {
  const t = useT(SETTINGS);
  const { area: areaName } = useSettingWords();
  const groups = useMemo(() => groupByArea(settings), [settings]);
  const [area, setArea] = useState(initialArea);
  const [query, setQuery] = useState(() => initialQuery ?? "");
  const [searchArea, setSearchArea] = useState<string | null>(null);
  const found = useMemo(() => searchSettings(settings, query), [settings, query]);
  const onQuery = (q: string) => {
    setQuery(q);
    if (q.trim() === "") setSearchArea(null);
  };
  const searching = query.trim() !== "";
  const cols = useMemo<ColumnDef<SettingItem, unknown>[]>(() => searching ? [
    { id: "area", header: t("colArea"), accessorKey: "area",
      cell: ({ getValue }) => <Badge tone="neutral" dot={false}>{areaName(String(getValue()))}</Badge> },
    ...columns,
  ] : columns, [searching, columns, t, areaName]);
  if (groups.length === 0) return null;
  const activeArea = groups.some((g) => g.area === area) ? area : groups[0].area;
  const pickedArea = searchAreaPicked(found, searchArea);
  const rows = searching ? matchesInArea(found, pickedArea) : groups.find((g) => g.area === activeArea)?.items ?? [];
  return (
    <>
      <SearchBox value={query} onChange={onQuery} />
      {searching
        ? <SearchAreas query={query} found={found} picked={pickedArea} onPick={setSearchArea} areaName={areaName} />
        : <Tabs variant="underline" ariaLabel={t("areasLabel")} active={activeArea} onChange={setArea}
          tabs={groups.map((g) => ({ key: g.area, label: areaName(g.area), count: g.items.length }))}
          testId="settings-areas" />}
      {rows.length > 0 && (
        <DataTable<SettingItem> tableId={`settings-${app}-${searching ? "search" : activeArea}`} data={rows}
          columns={cols} getRowId={(s) => s.key} searchable={false} paginate={false} tableMinWidth={980} />
      )}
    </>
  );
}
