"use client";
// app/admin/places/page.tsx — AA-847 places of the live catalog (ADR 0007 decision 10, design §5).
// Tabs:
//   Coverage — per country: live tours, built by the place pipeline, in the current graph, a stop on every
//     extracted day (GET /admin/places/coverage). The gate for removing TripPlanner's places fallback.
//   Content validity — the deterministic report (GET /admin/places/validity, server-side filters + pages):
//     duration mismatches, the same place twice in a tour, trails / rivers / parks stored as a pin, a component
//     its day text does not name. Each finding carries a reason code. Admins fix the not-a-pin and duplicate
//     findings (POST /admin/places/validity/fix): dry run first, then apply; every apply writes an audit row
//     and can be undone.
//   Lines — AA-847 (owner decision S246): rivers, roads and trails drawn as lines; the state of each line's geometry,
//     the shapes backfill (dry run first) and the manual GeoJSON exception path (LinesTab.tsx).
//   Merges (AA-847 Problem 2) — the automatic duplicate merge: allow-list, dry run approval, merge log with undo
//     (MergesTab.tsx).
// Geocoded points come from Geoapify over OpenStreetMap data: the attribution footer is required (ADR 0007).

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ColumnDef } from "@tanstack/react-table";
import { RefreshCw, MapPin, AlertTriangle, CheckCircle2, Route } from "lucide-react";
import {
  Button, Tabs, PageFrame, DataTable, PageHeader, useFormat, useT, Badge, Eyebrow, K, KpiTile, LoadingScreen,
  Section, mono,
} from "../../_kit";
import { PL } from "./messages";
import LinesTab from "./LinesTab";
import MergesTab from "./MergesTab";

type CoverageRow = {
  country: string; live: number; built: number; partial: number; in_graph: number; stop_every_day: number;
  build_cost_usd: number;
};
type CoverageTotals = Omit<CoverageRow, "country">;
type CheckCounts = { items: number; tours: number; reasons: Record<string, number> };
type Gate = { reasons: string[]; items: number; tours: number; pass: boolean };
type Bucket = { tours: number; tours_flagged: number; checks: Record<string, CheckCounts>; gate?: Gate };
type Item = {
  check: string; reason: string; country: string | null; source_id: string; tour_id: string; title: string | null;
  day: number | null; names: string[]; destination_ids: string[]; detail: string | null; keeper_id: string | null;
  km: number | null;
};
type Trend = { build_id: string; built_at: string | null; is_current: boolean; validity: Record<string, number> | null };
type Attribution = { geoapify: string; geoapify_url: string; osm: string; osm_url: string };
type Validity = {
  checks: string[]; reasons: Record<string, string[]>; summary: { catalog: Bucket; by_country: Record<string, Bucket> };
  trend: Trend[]; attribution: Attribution; total: number; page: number; page_size: number; items: Item[];
};
type FixChange = { destination_id: string; name: string; keeper_name?: string; before: object; after: object };
type FixResult = {
  check?: string; findings?: number; changes?: FixChange[]; skipped?: { name: string; why: string }[];
  cross_app_references?: Record<string, number | string>; dry_run: boolean; applied?: number; audit_id?: string;
  restored?: number; fingerprint?: string;
};

const PAGE_SIZE = 50;
const linkBtn = { border: 0, background: "none", color: K.accentDeep, cursor: "pointer", padding: 0, fontSize: 12 };
// Tab labels and check names are message keys (messages.ts).
const TABS = [{ key: "coverage", label: "tabCoverage" }, { key: "validity", label: "tabValidity" },
              { key: "lines", label: "tabLines" },
              { key: "merges", label: "tabMerges" }] as const;
const CHECK_LABEL: Record<string, "checkDuration" | "checkSamePlace" | "checkNotAPin" | "checkDayText"> = {
  duration: "checkDuration", same_place: "checkSamePlace", not_a_pin: "checkNotAPin", day_text: "checkDayText",
};
// the fix each reason can take (POST /admin/places/validity/fix `check`)
const FIX_OF: Record<string, string> = {
  area_feature: "not_a_pin", linear_feature: "linear_feature", near_duplicate: "duplicate_place",
};
const DEFAULT_ATTRIBUTION: Attribution = {
  geoapify: "Powered by Geoapify", geoapify_url: "https://www.geoapify.com/",
  osm: "© OpenStreetMap contributors", osm_url: "https://www.openstreetmap.org/copyright",
};

const pct = (a: number, b: number) => (b ? Math.round((a / b) * 100) : 0);
const selectStyle = { padding: "6px 8px", borderRadius: 8, border: `1px solid ${K.line}`, fontSize: 13 };

function Bar({ value, total, color = K.accent }: { value: number; total: number; color?: string }) {
  const p = pct(value, total);
  return (
    <div style={{ minWidth: 110 }}>
      <div style={{ fontSize: 11.5, fontFamily: mono }}>{value}/{total} · {p}%</div>
      <div style={{ height: 5, background: K.line2, borderRadius: 3, overflow: "hidden", marginTop: 3 }}>
        <div style={{ width: `${p}%`, height: "100%", background: color }} />
      </div>
    </div>
  );
}

function AttributionFooter({ a }: { a: Attribution }) {
  return (
    <div style={{ marginTop: 24, fontSize: 11.5, color: K.muted, display: "flex", gap: 12, flexWrap: "wrap" }}>
      <a href={a.geoapify_url} target="_blank" rel="noreferrer" style={{ color: K.muted }}>{a.geoapify}</a>
      <a href={a.osm_url} target="_blank" rel="noreferrer" style={{ color: K.muted }}>{a.osm}</a>
    </div>
  );
}

export default function PlacesPage() {
  const t = useT(PL);
  const { formatDateTime } = useFormat();
  const checkLabel = (c: string) => (CHECK_LABEL[c] ? t(CHECK_LABEL[c]) : c);
  // The loaders read the translator through a ref, so a language switch does not refetch.
  const tRef = useRef(t);
  useEffect(() => { tRef.current = t; }, [t]);
  const [tab, setTab] = useState("coverage");
  const [error, setError] = useState<string | null>(null);
  const [coverage, setCoverage] = useState<{ countries: CoverageRow[]; totals: CoverageTotals } | null>(null);
  const [validity, setValidity] = useState<Validity | null>(null);
  const [country, setCountry] = useState("");
  const [check, setCheck] = useState("");
  const [reason, setReason] = useState("");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(PAGE_SIZE);
  const [busy, setBusy] = useState<string | null>(null);
  const [fix, setFix] = useState<{ body: Record<string, unknown>; result: FixResult } | null>(null);

  const loadCoverage = useCallback(async () => {
    const r = await fetch("/api/admin/places/coverage");
    if (!r.ok) { setError(tRef.current("coverageFailed", r.status)); return; }
    setCoverage(await r.json());
  }, []);

  const loadValidity = useCallback(async () => {
    const qs = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
    if (country) qs.set("country", country);
    if (check) qs.set("check", check);
    if (reason) qs.set("reason", reason);
    const r = await fetch(`/api/admin/places/validity?${qs}`);
    if (!r.ok) { setError(tRef.current("validityFailed", r.status)); return; }
    setValidity(await r.json());
  }, [country, check, reason, page, pageSize]);

  // Fetch on mount / tab / filter change (the loaders set state only after their fetch resolves).
  useEffect(() => { loadCoverage(); }, [loadCoverage]);
  useEffect(() => { if (tab === "validity") loadValidity(); }, [tab, loadValidity]);

  async function runFix(body: Record<string, unknown>) {
    setBusy(JSON.stringify(body));
    try {
      const r = await fetch("/api/admin/places/validity/fix", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
      const out = await r.json().catch(() => ({}));
      if (!r.ok) { setError(t("fixFailed", JSON.stringify(out.detail ?? r.status))); return; }
      setFix({ body, result: out });
      if (body.dry_run === false) await Promise.all([loadValidity(), loadCoverage()]);
    } finally { setBusy(null); }
  }

  const coverageColumns = useMemo<ColumnDef<CoverageRow, unknown>[]>(() => [
    { accessorKey: "country", header: t("country") },
    { accessorKey: "live", header: t("live"), meta: { align: "right" } },
    { accessorKey: "built", header: t("built"), cell: ({ row }) => <Bar value={row.original.built} total={row.original.live} color={K.success} /> },
    { accessorKey: "in_graph", header: t("inGraph"), cell: ({ row }) => <Bar value={row.original.in_graph} total={row.original.live} /> },
    { accessorKey: "stop_every_day", header: t("stopEveryDay"),
      cell: ({ row }) => <Bar value={row.original.stop_every_day} total={row.original.live} color={K.accent} /> },
    { accessorKey: "build_cost_usd", header: t("cost"), meta: { align: "right" },
      cell: ({ row }) => <span style={{ fontFamily: mono }}>${row.original.build_cost_usd.toFixed(2)}</span> },
  ], [t]);

  const itemColumns = useMemo<ColumnDef<Item, unknown>[]>(() => [
    { id: "tour", header: t("tour"), accessorFn: it => it.title ?? it.tour_id,
      cell: ({ row }) => <>{row.original.title ?? row.original.tour_id}
        <div style={{ fontSize: 11, color: K.muted, fontFamily: mono }}>{row.original.source_id.slice(0, 8)}</div></> },
    { accessorKey: "country", header: t("country"), cell: ({ row }) => row.original.country ?? "—" },
    { accessorKey: "check", header: t("check"), cell: ({ row }) => checkLabel(row.original.check) },
    { accessorKey: "reason", header: t("reason"),
      cell: ({ row }) => <Badge tone={FIX_OF[row.original.reason] ? "warning" : "neutral"}>{row.original.reason}</Badge> },
    { accessorKey: "day", header: t("day"), meta: { align: "right" }, cell: ({ row }) => row.original.day ?? "—" },
    { id: "places", header: t("places"), accessorFn: it => it.names.join(" · "),
      cell: ({ row }) => <>{row.original.names.join(" · ") || "—"}
        {row.original.km != null && <span style={{ color: K.muted }}> ({row.original.km} km)</span>}</> },
    { id: "detail", header: t("detail"), accessorFn: it => it.detail ?? "",
      cell: ({ row }) => {
        const it = row.original;
        return <span style={{ fontSize: 12, color: K.body }}>{it.detail ?? ""}
          {FIX_OF[it.reason] && (
            <div><button disabled={!!busy} style={linkBtn}
                         onClick={() => runFix({ check: FIX_OF[it.reason], source_id: it.source_id, country: it.country, dry_run: true })}>
              {t("previewFixTour")}</button></div>
          )}</span>;
      } },
  // eslint-disable-next-line react-hooks/exhaustive-deps -- runFix only reads state setters and the loaders
  ], [busy, t]);

  if (!coverage) return <PageFrame><LoadingScreen message={t("loadingPlaces")} /></PageFrame>;
  const tot = coverage.totals;
  const countries = coverage.countries.map(c => c.country);
  const cat = validity?.summary.catalog;
  const reasons = check && validity ? validity.reasons[check] ?? [] : Object.values(validity?.reasons ?? {}).flat();

  return (
    <PageFrame>
      <PageHeader
        title={t("title")}
        description={t("description")}
        actions={<Button onClick={() => { loadCoverage(); if (tab === "validity") loadValidity(); }} size="sm">
          <RefreshCw size={13} /> {t("refresh")}</Button>}
      />

      {error && (
        <Section style={{ marginBottom: 16, borderColor: K.dangerBorder, background: K.dangerTint }}>
          <span style={{ color: K.danger, fontSize: 13 }}>{error}</span>{" "}
          <button onClick={() => setError(null)} style={{ border: 0, background: "none", cursor: "pointer", color: K.muted }}>{t("dismiss")}</button>
        </Section>
      )}

      <div style={{ marginBottom: 18 }}><Tabs tabs={TABS.map((x) => ({ key: x.key, label: t(x.label) }))} active={tab} onChange={setTab} /></div>

      {tab === "coverage" && (
        <>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(190px, 1fr))", gap: 14, marginBottom: 18 }}>
            <KpiTile label={t("liveTours")} value={String(tot.live)} icon={<MapPin size={16} />} hint={t("countriesCount", countries.length)} />
            <KpiTile label={t("built")} value={`${pct(tot.built, tot.live)}%`} icon={<CheckCircle2 size={16} />} hintTone="success"
                      hint={t("builtSub", tot.built, tot.partial)} />
            <KpiTile label={t("inTheGraph")} value={`${pct(tot.in_graph, tot.live)}%`} icon={<Route size={16} />}
                      hint={t("inGraphSub", tot.in_graph)} />
            <KpiTile label={t("stopEveryDay")} value={`${pct(tot.stop_every_day, tot.live)}%`} icon={<AlertTriangle size={16} />}
                      hintTone={tot.stop_every_day < tot.live ? "warning" : "success"} hint={t("gateSub")} />
            <KpiTile label={t("buildCost")} value={`$${tot.build_cost_usd.toFixed(2)}`} icon={<MapPin size={16} />} hint="geo.tour_build" />
          </div>
          <DataTable<CoverageRow> tableId="admin-places-coverage" data={coverage.countries} columns={coverageColumns}
                                  getRowId={r => r.country} searchable searchPlaceholder={t("country")} enableCsv
                                  csvFilename="places-coverage.csv" emptyTitle={t("noLiveTours")} />
        </>
      )}

      {tab === "validity" && !validity && <LoadingScreen message={t("runningValidity")} />}
      {tab === "validity" && validity && cat && (
        <>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))", gap: 14, marginBottom: 18 }}>
            <KpiTile label={t("toursFlagged")} value={`${cat.tours_flagged}/${cat.tours}`} icon={<AlertTriangle size={16} />}
                      hintTone={cat.tours_flagged ? "warning" : "success"} hint={country || t("wholeCatalog")} />
            {cat.gate && (
              <KpiTile label={t("flipGate")} value={cat.gate.pass ? t("pass") : t("blocking", cat.gate.items)}
                        icon={cat.gate.pass ? <CheckCircle2 size={16} /> : <AlertTriangle size={16} />}
                        hintTone={cat.gate.pass ? "success" : "warning"}
                        hint={t("gateTours", cat.gate.tours, cat.gate.reasons.join(", "))} />
            )}
            {validity.checks.map(ch => (
              <KpiTile key={ch} label={checkLabel(ch)} value={String(cat.checks[ch]?.items ?? 0)}
                        hint={t("toursN", cat.checks[ch]?.tours ?? 0)} />
            ))}
          </div>

          <Section style={{ marginBottom: 16 }} bodyStyle={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center" }}>
            <select aria-label={t("country")} value={country} onChange={e => { setCountry(e.target.value); setPage(1); }} style={selectStyle}>
              <option value="">{t("allCountries")}</option>
              {countries.map(c => <option key={c} value={c}>{c}</option>)}
            </select>
            <select aria-label={t("check")} value={check} onChange={e => { setCheck(e.target.value); setReason(""); setPage(1); }} style={selectStyle}>
              <option value="">{t("allChecks")}</option>
              {validity.checks.map(c => <option key={c} value={c}>{checkLabel(c)}</option>)}
            </select>
            <select aria-label={t("reason")} value={reason} onChange={e => { setReason(e.target.value); setPage(1); }} style={selectStyle}>
              <option value="">{t("allReasons")}</option>
              {reasons.map(r => <option key={r} value={r}>{r}</option>)}
            </select>
            <span style={{ fontSize: 12.5, color: K.muted }}>{t("findings", validity.total)}</span>
            {reason && FIX_OF[reason] && (
              <Button size="sm" disabled={!!busy}
                   onClick={() => runFix({ check: FIX_OF[reason], country: country || null, dry_run: true })}>
                {t("previewFix", `${reason}${country ? `, ${country}` : ""}`)}
              </Button>
            )}
          </Section>

          {fix && (
            <Section style={{ marginBottom: 16 }}>
              <Eyebrow>{fix.result.dry_run ? t("dryRun") : t("applied")} — {fix.result.check ?? t("undo")}</Eyebrow>
              <div style={{ fontSize: 13, color: K.body, marginBottom: 8 }}>
                {fix.result.restored !== undefined
                  ? t("rowsRestored", fix.result.restored, fix.result.dry_run)
                  : t("placesChanged", fix.result.changes?.length ?? 0, fix.result.dry_run)}
                {fix.result.audit_id && <> · {t("audit")} <span style={{ fontFamily: mono }}>{fix.result.audit_id.slice(0, 8)}</span></>}
                {!!fix.result.skipped?.length && t("skipped", fix.result.skipped.length)}
              </div>
              {(fix.result.changes ?? []).slice(0, 20).map(c => (
                <div key={c.destination_id} style={{ fontSize: 12.5, fontFamily: mono, color: K.body }}>
                  {c.name}{c.keeper_name ? ` → ${c.keeper_name}` : ""}: {JSON.stringify(c.before)} → {JSON.stringify(c.after)}
                </div>
              ))}
              {fix.result.cross_app_references && Object.keys(fix.result.cross_app_references).length > 0 && (
                <div style={{ fontSize: 12, color: K.muted, marginTop: 8 }}>
                  {t("otherApps")}{" "}
                  {Object.entries(fix.result.cross_app_references).map(([k, v]) => `${k}: ${v}`).join(" · ")}
                </div>
              )}
              <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
                {fix.result.dry_run && ((fix.result.changes?.length ?? 0) > 0 || (fix.result.restored ?? 0) > 0) && (
                  <Button variant="primary" size="sm" disabled={!!busy}
                       onClick={() => { if (window.confirm(t("confirmFix"))) runFix({ ...fix.body, dry_run: false, fingerprint: fix.result.fingerprint }); }}>
                    {t("apply")}
                  </Button>
                )}
                {!fix.result.dry_run && fix.result.audit_id && fix.result.restored === undefined && (
                  <Button size="sm" disabled={!!busy} onClick={() => runFix({ undo: fix.result.audit_id, dry_run: true })}>{t("undoEllipsis")}</Button>
                )}
                <Button size="sm" onClick={() => setFix(null)}>{t("close")}</Button>
              </div>
            </Section>
          )}

          <DataTable<Item> tableId="admin-places-validity" data={validity.items} columns={itemColumns}
                           getRowId={it => `${it.source_id}-${it.check}-${it.reason}-${it.day ?? ""}-${it.names.join("|")}`}
                           serverPagination={{ pageIndex: validity.page - 1, pageSize: validity.page_size, total: validity.total,
                                               onChange: (pi, ps) => { setPage(ps !== pageSize ? 1 : pi + 1); setPageSize(ps); } }}
                           enableCsv csvFilename="places-validity.csv"
                           emptyTitle={t("noFindings")} emptyDescription={t("noFindingsDesc")} />

          {validity.trend.length > 0 && (
            <Section style={{ marginTop: 16 }}>
              <Eyebrow>{t("lastBuilds")}</Eyebrow>
              {validity.trend.map(b => (
                <div key={b.build_id} style={{ fontSize: 12.5, color: K.body, fontFamily: mono }}>
                  {formatDateTime(b.built_at)} {b.is_current ? t("current") : ""} ·{" "}
                  {b.validity ? t("flaggedOf", String(b.validity.tours_flagged ?? "?"), String(b.validity.tours ?? "?")) : t("noValidity")}
                </div>
              ))}
            </Section>
          )}
        </>
      )}

      {tab === "lines" && <LinesTab countries={countries} onError={setError} />}
      {tab === "merges" && <MergesTab onError={setError} />}

      <AttributionFooter a={validity?.attribution ?? DEFAULT_ATTRIBUTION} />
    </PageFrame>
  );
}
