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
// Geocoded points come from Geoapify over OpenStreetMap data: the attribution footer is required (ADR 0007).

import { useCallback, useEffect, useMemo, useState } from "react";
import type { ColumnDef } from "@tanstack/react-table";
import { RefreshCw, MapPin, AlertTriangle, CheckCircle2, Route } from "lucide-react";
import { A, mono, Card, SLabel, Badge, Btn, LoadingScreen, StatCard, TabBar } from "../_components/adminUi";
import { DataTable, PageHeader, formatDateTime } from "../../_kit";

type CoverageRow = {
  country: string; live: number; built: number; partial: number; in_graph: number; stop_every_day: number;
  build_cost_usd: number;
};
type CoverageTotals = Omit<CoverageRow, "country">;
type CheckCounts = { items: number; tours: number; reasons: Record<string, number> };
type Bucket = { tours: number; tours_flagged: number; checks: Record<string, CheckCounts> };
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
  restored?: number;
};

const PAGE_SIZE = 50;
const linkBtn = { border: 0, background: "none", color: A.accentDeep, cursor: "pointer", padding: 0, fontSize: 12 };
const TABS = [{ key: "coverage", label: "Coverage" }, { key: "validity", label: "Content validity" }];
const CHECK_LABEL: Record<string, string> = {
  duration: "Duration mismatch", same_place: "Same place twice", not_a_pin: "Not a pin", day_text: "Day text",
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
const selectStyle = { padding: "6px 8px", borderRadius: 8, border: `1px solid ${A.line}`, fontSize: 13 };

function Bar({ value, total, color = A.accent }: { value: number; total: number; color?: string }) {
  const p = pct(value, total);
  return (
    <div style={{ minWidth: 110 }}>
      <div style={{ fontSize: 11.5, fontFamily: mono }}>{value}/{total} · {p}%</div>
      <div style={{ height: 5, background: A.line2, borderRadius: 3, overflow: "hidden", marginTop: 3 }}>
        <div style={{ width: `${p}%`, height: "100%", background: color }} />
      </div>
    </div>
  );
}

function AttributionFooter({ a }: { a: Attribution }) {
  return (
    <div style={{ marginTop: 24, fontSize: 11.5, color: A.muted, display: "flex", gap: 12, flexWrap: "wrap" }}>
      <a href={a.geoapify_url} target="_blank" rel="noreferrer" style={{ color: A.muted }}>{a.geoapify}</a>
      <a href={a.osm_url} target="_blank" rel="noreferrer" style={{ color: A.muted }}>{a.osm}</a>
    </div>
  );
}

export default function PlacesPage() {
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
    if (!r.ok) { setError(`Coverage failed (${r.status})`); return; }
    setCoverage(await r.json());
  }, []);

  const loadValidity = useCallback(async () => {
    const qs = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
    if (country) qs.set("country", country);
    if (check) qs.set("check", check);
    if (reason) qs.set("reason", reason);
    const r = await fetch(`/api/admin/places/validity?${qs}`);
    if (!r.ok) { setError(`Validity report failed (${r.status})`); return; }
    setValidity(await r.json());
  }, [country, check, reason, page, pageSize]);

  /* eslint-disable react-hooks/set-state-in-effect -- fetch on mount / tab / filter change, same pattern as every admin page */
  useEffect(() => { loadCoverage(); }, [loadCoverage]);
  useEffect(() => { if (tab === "validity") loadValidity(); }, [tab, loadValidity]);
  /* eslint-enable react-hooks/set-state-in-effect */

  async function runFix(body: Record<string, unknown>) {
    setBusy(JSON.stringify(body));
    try {
      const r = await fetch("/api/admin/places/validity/fix", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
      const out = await r.json().catch(() => ({}));
      if (!r.ok) { setError(`Fix failed: ${JSON.stringify(out.detail ?? r.status)}`); return; }
      setFix({ body, result: out });
      if (body.dry_run === false) await Promise.all([loadValidity(), loadCoverage()]);
    } finally { setBusy(null); }
  }

  const coverageColumns = useMemo<ColumnDef<CoverageRow, unknown>[]>(() => [
    { accessorKey: "country", header: "Country" },
    { accessorKey: "live", header: "Live", meta: { align: "right" } },
    { accessorKey: "built", header: "Built", cell: ({ row }) => <Bar value={row.original.built} total={row.original.live} color={A.green} /> },
    { accessorKey: "in_graph", header: "In graph", cell: ({ row }) => <Bar value={row.original.in_graph} total={row.original.live} /> },
    { accessorKey: "stop_every_day", header: "Stop every day",
      cell: ({ row }) => <Bar value={row.original.stop_every_day} total={row.original.live} color={A.gold} /> },
    { accessorKey: "build_cost_usd", header: "Cost", meta: { align: "right" },
      cell: ({ row }) => <span style={{ fontFamily: mono }}>${row.original.build_cost_usd.toFixed(2)}</span> },
  ], []);

  const itemColumns = useMemo<ColumnDef<Item, unknown>[]>(() => [
    { id: "tour", header: "Tour", accessorFn: it => it.title ?? it.tour_id,
      cell: ({ row }) => <>{row.original.title ?? row.original.tour_id}
        <div style={{ fontSize: 11, color: A.muted, fontFamily: mono }}>{row.original.source_id.slice(0, 8)}</div></> },
    { accessorKey: "country", header: "Country", cell: ({ row }) => row.original.country ?? "—" },
    { accessorKey: "check", header: "Check", cell: ({ row }) => CHECK_LABEL[row.original.check] ?? row.original.check },
    { accessorKey: "reason", header: "Reason",
      cell: ({ row }) => <Badge color={FIX_OF[row.original.reason] ? "amber" : "gray"}>{row.original.reason}</Badge> },
    { accessorKey: "day", header: "Day", meta: { align: "right" }, cell: ({ row }) => row.original.day ?? "—" },
    { id: "places", header: "Places", accessorFn: it => it.names.join(" · "),
      cell: ({ row }) => <>{row.original.names.join(" · ") || "—"}
        {row.original.km != null && <span style={{ color: A.muted }}> ({row.original.km} km)</span>}</> },
    { id: "detail", header: "Detail", accessorFn: it => it.detail ?? "",
      cell: ({ row }) => {
        const it = row.original;
        return <span style={{ fontSize: 12, color: A.body }}>{it.detail ?? ""}
          {FIX_OF[it.reason] && (
            <div><button disabled={!!busy} style={linkBtn}
                         onClick={() => runFix({ check: FIX_OF[it.reason], source_id: it.source_id, country: it.country, dry_run: true })}>
              preview fix for this tour</button></div>
          )}</span>;
      } },
  // eslint-disable-next-line react-hooks/exhaustive-deps -- runFix only reads state setters and the loaders
  ], [busy]);

  if (!coverage) return <LoadingScreen msg="Loading places..." />;
  const t = coverage.totals;
  const countries = coverage.countries.map(c => c.country);
  const cat = validity?.summary.catalog;
  const reasons = check && validity ? validity.reasons[check] ?? [] : Object.values(validity?.reasons ?? {}).flat();

  return (
    <main className="aa-admin-main" style={{ flex: 1, padding: "32px 36px", minWidth: 0, minHeight: 0, overflowY: "auto" }}>
      <PageHeader
        title="Places"
        description="Where the live tours go on the map — the place pipeline's coverage per country and the content-validity report the TripPlanner map switch waits on (no model call)."
        actions={<Btn onClick={() => { loadCoverage(); if (tab === "validity") loadValidity(); }} size="sm">
          <RefreshCw size={13} /> Refresh</Btn>}
      />

      {error && (
        <Card style={{ marginBottom: 16, borderColor: A.redBorder, background: A.redTint }}>
          <span style={{ color: A.red, fontSize: 13 }}>{error}</span>{" "}
          <button onClick={() => setError(null)} style={{ border: 0, background: "none", cursor: "pointer", color: A.muted }}>dismiss</button>
        </Card>
      )}

      <div style={{ marginBottom: 18 }}><TabBar tabs={TABS} active={tab} onChange={setTab} /></div>

      {tab === "coverage" && (
        <>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(190px, 1fr))", gap: 14, marginBottom: 18 }}>
            <StatCard label="Live tours" value={String(t.live)} icon={<MapPin size={16} />} sub={`${countries.length} countries`} />
            <StatCard label="Built" value={`${pct(t.built, t.live)}%`} icon={<CheckCircle2 size={16} />} accent={A.green}
                      sub={`${t.built} tours · ${t.partial} partial`} />
            <StatCard label="In the graph" value={`${pct(t.in_graph, t.live)}%`} icon={<Route size={16} />}
                      sub={`${t.in_graph} tours have stops`} />
            <StatCard label="Stop every day" value={`${pct(t.stop_every_day, t.live)}%`} icon={<AlertTriangle size={16} />}
                      accent={t.stop_every_day < t.live ? A.gold : A.green} sub="the fallback-removal gate" />
            <StatCard label="Build cost" value={`$${t.build_cost_usd.toFixed(2)}`} icon={<MapPin size={16} />} sub="geo.tour_build" />
          </div>
          <DataTable<CoverageRow> tableId="admin-places-coverage" data={coverage.countries} columns={coverageColumns}
                                  getRowId={r => r.country} searchable searchPlaceholder="Country" enableCsv
                                  csvFilename="places-coverage.csv" emptyTitle="No live tours" />
        </>
      )}

      {tab === "validity" && !validity && <LoadingScreen msg="Running the validity report..." />}
      {tab === "validity" && validity && cat && (
        <>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))", gap: 14, marginBottom: 18 }}>
            <StatCard label="Tours flagged" value={`${cat.tours_flagged}/${cat.tours}`} icon={<AlertTriangle size={16} />}
                      accent={cat.tours_flagged ? A.gold : A.green} sub={country || "whole catalog"} />
            {validity.checks.map(ch => (
              <StatCard key={ch} label={CHECK_LABEL[ch] ?? ch} value={String(cat.checks[ch]?.items ?? 0)}
                        sub={`${cat.checks[ch]?.tours ?? 0} tours`} />
            ))}
          </div>

          <Card style={{ marginBottom: 16, display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center" }}>
            <select aria-label="Country" value={country} onChange={e => { setCountry(e.target.value); setPage(1); }} style={selectStyle}>
              <option value="">All countries</option>
              {countries.map(c => <option key={c} value={c}>{c}</option>)}
            </select>
            <select aria-label="Check" value={check} onChange={e => { setCheck(e.target.value); setReason(""); setPage(1); }} style={selectStyle}>
              <option value="">All checks</option>
              {validity.checks.map(c => <option key={c} value={c}>{CHECK_LABEL[c] ?? c}</option>)}
            </select>
            <select aria-label="Reason" value={reason} onChange={e => { setReason(e.target.value); setPage(1); }} style={selectStyle}>
              <option value="">All reasons</option>
              {reasons.map(r => <option key={r} value={r}>{r}</option>)}
            </select>
            <span style={{ fontSize: 12.5, color: A.muted }}>{validity.total} findings</span>
            {reason && FIX_OF[reason] && (
              <Btn size="sm" disabled={!!busy}
                   onClick={() => runFix({ check: FIX_OF[reason], country: country || null, dry_run: true })}>
                Preview fix ({reason}{country ? `, ${country}` : ""})
              </Btn>
            )}
          </Card>

          {fix && (
            <Card style={{ marginBottom: 16 }}>
              <SLabel>{fix.result.dry_run ? "Dry run" : "Applied"} — {fix.result.check ?? "undo"}</SLabel>
              <div style={{ fontSize: 13, color: A.body, marginBottom: 8 }}>
                {fix.result.restored !== undefined
                  ? `${fix.result.restored} rows ${fix.result.dry_run ? "would be restored" : "restored"}`
                  : `${fix.result.changes?.length ?? 0} places ${fix.result.dry_run ? "would change" : "changed"}`}
                {fix.result.audit_id && <> · audit <span style={{ fontFamily: mono }}>{fix.result.audit_id.slice(0, 8)}</span></>}
                {!!fix.result.skipped?.length && ` · ${fix.result.skipped.length} skipped (admin-fixed)`}
              </div>
              {(fix.result.changes ?? []).slice(0, 20).map(c => (
                <div key={c.destination_id} style={{ fontSize: 12.5, fontFamily: mono, color: A.body }}>
                  {c.name}{c.keeper_name ? ` → ${c.keeper_name}` : ""}: {JSON.stringify(c.before)} → {JSON.stringify(c.after)}
                </div>
              ))}
              {fix.result.cross_app_references && Object.keys(fix.result.cross_app_references).length > 0 && (
                <div style={{ fontSize: 12, color: A.muted, marginTop: 8 }}>
                  Other apps still pointing at the merged places (reported, never written):{" "}
                  {Object.entries(fix.result.cross_app_references).map(([k, v]) => `${k}: ${v}`).join(" · ")}
                </div>
              )}
              <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
                {fix.result.dry_run && ((fix.result.changes?.length ?? 0) > 0 || (fix.result.restored ?? 0) > 0) && (
                  <Btn variant="primary" size="sm" disabled={!!busy}
                       onClick={() => { if (window.confirm("Apply this fix? It is audited and can be undone.")) runFix({ ...fix.body, dry_run: false }); }}>
                    Apply
                  </Btn>
                )}
                {!fix.result.dry_run && fix.result.audit_id && fix.result.restored === undefined && (
                  <Btn size="sm" disabled={!!busy} onClick={() => runFix({ undo: fix.result.audit_id, dry_run: true })}>Undo…</Btn>
                )}
                <Btn size="sm" onClick={() => setFix(null)}>Close</Btn>
              </div>
            </Card>
          )}

          <DataTable<Item> tableId="admin-places-validity" data={validity.items} columns={itemColumns}
                           getRowId={it => `${it.source_id}-${it.check}-${it.reason}-${it.day ?? ""}-${it.names.join("|")}`}
                           serverPagination={{ pageIndex: validity.page - 1, pageSize: validity.page_size, total: validity.total,
                                               onChange: (pi, ps) => { setPage(ps !== pageSize ? 1 : pi + 1); setPageSize(ps); } }}
                           enableCsv csvFilename="places-validity.csv"
                           emptyTitle="No findings" emptyDescription="Nothing to fix for these filters." />

          {validity.trend.length > 0 && (
            <Card style={{ marginTop: 16 }}>
              <SLabel>Last graph builds</SLabel>
              {validity.trend.map(b => (
                <div key={b.build_id} style={{ fontSize: 12.5, color: A.body, fontFamily: mono }}>
                  {formatDateTime(b.built_at)} {b.is_current ? "(current)" : ""} ·{" "}
                  {b.validity ? `${b.validity.tours_flagged ?? "?"} of ${b.validity.tours ?? "?"} tours flagged` : "no validity counts"}
                </div>
              ))}
            </Card>
          )}
        </>
      )}

      <AttributionFooter a={validity?.attribution ?? DEFAULT_ATTRIBUTION} />
    </main>
  );
}
