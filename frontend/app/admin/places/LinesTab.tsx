"use client";
// app/admin/places/LinesTab.tsx — AA-847 (owner decision S246, D1): rivers, roads and trekking stretches are DRAWN
// on the TripPlanner map as lines — never a pin, a circle or hidden. This tab lists every line place and the state of
// its geometry (geo.place_shape): ok (an OpenStreetMap feature), approximate (no single OSM feature: the anchored
// route of a tour-day, drawn dashed "approximate route"), pending / not_found / failed (TripPlanner draws the
// provisional dashed line between the day's stops meanwhile), manual (an admin's GeoJSON — the exception path).
//   GET  /admin/places/lines?country=&status=
//   POST /admin/places/shapes/backfill   dry run first (names + Geoapify credit estimate), then apply (≤ 50 names)
//   PUT / DELETE /admin/places/{id}/shape  manual GeoJSON

import { useCallback, useEffect, useMemo, useState } from "react";
import type { ColumnDef } from "@tanstack/react-table";
import { AlertTriangle, CheckCircle2, Route, Waves } from "lucide-react";
import { A, mono, Card, SLabel, Badge, Btn, LoadingScreen, StatCard } from "../_components/adminUi";
import { DataTable, formatDateTime } from "../../_kit";

export type LineItem = {
  destination_id: string; name: string; country: string; status: string; source: string | null;
  length_km: number | null; osm_type: string | null; osm_id: number | null; tries: number | null;
  next_retry_at: string | null; last_error: string | null; tour_days: number;
};
type Lines = {
  enabled: boolean; enabled_env: string; lines: number; drawn: number; totals: Record<string, number>;
  approximate_by_country: Record<string, number>; items: LineItem[];
};
type Backfill = {
  dry_run: boolean; provider: string; enabled: boolean; would_refuse?: string[]; forced?: number; job?: string | null;
  estimate: { names: number; geoapify_credits_max: number; overpass_queries_max: number; credits_used_today: number;
              daily_credit_limit: number; credits_note: string };
  items: { destination_id: string; name: string; country: string; status: string }[];
};

const STATUSES = ["missing", "pending", "ok", "approximate", "not_found", "failed", "manual"];
const STATUS_COLOR: Record<string, "green" | "amber" | "gray" | "red" | "blue"> = {
  ok: "green", manual: "blue", approximate: "amber", pending: "gray", missing: "red", not_found: "red", failed: "red",
};
const selectStyle = { padding: "6px 8px", borderRadius: 8, border: `1px solid ${A.line}`, fontSize: 13 };
const linkBtn = { border: 0, background: "none", color: A.accentDeep, cursor: "pointer", padding: 0, fontSize: 12 };

export default function LinesTab({ countries, onError }: { countries: string[]; onError: (m: string) => void }) {
  const [data, setData] = useState<Lines | null>(null);
  const [country, setCountry] = useState("");
  const [status, setStatus] = useState("");
  const [busy, setBusy] = useState(false);
  const [backfill, setBackfill] = useState<{ body: Record<string, unknown>; result: Backfill } | null>(null);
  const [manual, setManual] = useState<{ item: LineItem; text: string } | null>(null);

  const load = useCallback(async () => {
    const qs = new URLSearchParams();
    if (country) qs.set("country", country);
    if (status) qs.set("status", status);
    const r = await fetch(`/api/admin/places/lines?${qs}`);
    if (!r.ok) { onError(`Lines failed (${r.status})`); return; }
    setData(await r.json());
  }, [country, status, onError]);

  /* eslint-disable react-hooks/set-state-in-effect -- fetch on mount / filter change, same pattern as every admin page */
  useEffect(() => { load(); }, [load]);
  /* eslint-enable react-hooks/set-state-in-effect */

  async function runBackfill(body: Record<string, unknown>) {
    setBusy(true);
    try {
      const r = await fetch("/api/admin/places/shapes/backfill", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
      const out = await r.json().catch(() => ({}));
      if (!r.ok) { onError(`Backfill refused: ${JSON.stringify(out.detail ?? r.status)}`); return; }
      setBackfill({ body, result: out });
      if (body.dry_run === false) await load();
    } finally { setBusy(false); }
  }

  async function saveManual() {
    if (!manual) return;
    let geojson: unknown;
    try { geojson = JSON.parse(manual.text); } catch { onError("The GeoJSON is not valid JSON"); return; }
    setBusy(true);
    try {
      const r = await fetch(`/api/admin/places/${manual.item.destination_id}/shape`, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ geojson }),
      });
      const out = await r.json().catch(() => ({}));
      if (!r.ok) { onError(`Manual shape refused: ${JSON.stringify(out.detail ?? r.status)}`); return; }
      setManual(null);
      await load();
    } finally { setBusy(false); }
  }

  async function clearManual(item: LineItem) {
    if (!window.confirm(`Drop the manual line of ${item.name}? The job fetches it again.`)) return;
    const r = await fetch(`/api/admin/places/${item.destination_id}/shape`, { method: "DELETE" });
    if (!r.ok) { onError(`Clear failed (${r.status})`); return; }
    await load();
  }

  const columns = useMemo<ColumnDef<LineItem, unknown>[]>(() => [
    { accessorKey: "name", header: "Line place" },
    { accessorKey: "country", header: "Country" },
    { accessorKey: "status", header: "Geometry",
      cell: ({ row }) => <Badge color={STATUS_COLOR[row.original.status] ?? "gray"}>{row.original.status}</Badge> },
    { accessorKey: "source", header: "Source", cell: ({ row }) => row.original.source ?? "—" },
    { accessorKey: "length_km", header: "Length", meta: { align: "right" },
      cell: ({ row }) => row.original.length_km != null
        ? <span style={{ fontFamily: mono }}>{row.original.length_km.toFixed(1)} km</span> : "—" },
    { id: "osm", header: "OSM", accessorFn: it => it.osm_id ?? "",
      cell: ({ row }) => row.original.osm_id
        ? <a href={`https://www.openstreetmap.org/${row.original.osm_type}/${row.original.osm_id}`} target="_blank"
             rel="noreferrer" style={{ fontFamily: mono, fontSize: 12 }}>{row.original.osm_type}/{row.original.osm_id}</a>
        : "—" },
    { accessorKey: "tour_days", header: "Tour-days", meta: { align: "right" } },
    { id: "retry", header: "Tries / next", accessorFn: it => it.next_retry_at ?? "",
      cell: ({ row }) => <span style={{ fontSize: 12 }}>{row.original.tries ?? 0} · {formatDateTime(row.original.next_retry_at)}
        {row.original.last_error && <div style={{ color: A.muted }}>{row.original.last_error}</div>}</span> },
    { id: "act", header: "", accessorFn: () => "",
      cell: ({ row }) => {
        const it = row.original;
        return <span style={{ display: "flex", gap: 10 }}>
          {it.status !== "manual" && it.status !== "ok" && (
            <button disabled={busy} style={linkBtn}
                    onClick={() => runBackfill({ destination_ids: [it.destination_id], dry_run: true,
                                                 statuses: STATUSES.filter(s => s !== "manual") })}>fetch…</button>
          )}
          <button disabled={busy} style={linkBtn} onClick={() => setManual({ item: it, text: "" })}>set GeoJSON…</button>
          {it.status === "manual" && <button disabled={busy} style={linkBtn} onClick={() => clearManual(it)}>clear manual</button>}
        </span>;
      } },
  // eslint-disable-next-line react-hooks/exhaustive-deps -- the handlers only read state setters and load
  ], [busy]);

  if (!data) return <LoadingScreen msg="Loading line places..." />;
  const approx = Object.entries(data.approximate_by_country);
  const waiting = (data.totals.pending ?? 0) + (data.totals.missing ?? 0) + (data.totals.not_found ?? 0) + (data.totals.failed ?? 0);

  return (
    <>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 14, marginBottom: 18 }}>
        <StatCard label="Line places" value={String(data.lines)} icon={<Route size={16} />} sub={country || "whole catalog"} />
        <StatCard label="Drawn" value={`${data.drawn}/${data.lines}`} icon={<CheckCircle2 size={16} />}
                  accent={data.drawn === data.lines ? A.green : A.gold} sub="ok · manual · approximate" />
        <StatCard label="Approximate routes" value={String(data.totals.approximate ?? 0)} icon={<Waves size={16} />}
                  accent={A.gold} sub={approx.length ? approx.map(([c, n]) => `${c} ${n}`).join(" · ") : "none"} />
        <StatCard label="Waiting for a shape" value={String(waiting)} icon={<AlertTriangle size={16} />}
                  accent={waiting ? A.gold : A.green} sub="drawn as a provisional dashed line meanwhile" />
        <StatCard label="Shape job" value={data.enabled ? "On" : "Off"} icon={<Route size={16} />}
                  accent={data.enabled ? A.green : A.gold} sub={data.enabled_env} />
      </div>

      <Card style={{ marginBottom: 16, display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center" }}>
        <select aria-label="Country" value={country} onChange={e => setCountry(e.target.value)} style={selectStyle}>
          <option value="">All countries</option>
          {countries.map(c => <option key={c} value={c}>{c}</option>)}
        </select>
        <select aria-label="Geometry status" value={status} onChange={e => setStatus(e.target.value)} style={selectStyle}>
          <option value="">All statuses</option>
          {STATUSES.map(s => <option key={s} value={s}>{s}</option>)}
        </select>
        <Btn size="sm" disabled={busy} onClick={() => runBackfill({ country: country || null, limit: 50, dry_run: true })}>
          Preview shapes backfill{country ? ` (${country})` : ""}
        </Btn>
      </Card>

      {backfill && (
        <Card style={{ marginBottom: 16 }}>
          <SLabel>{backfill.result.dry_run ? "Dry run" : "Queued"} — shapes backfill ({backfill.result.provider})</SLabel>
          <div style={{ fontSize: 13, color: A.body }}>
            {backfill.result.estimate.names} names · ≤ {backfill.result.estimate.geoapify_credits_max} Geoapify credits ·
            ≤ {backfill.result.estimate.overpass_queries_max} Overpass queries (free) · today{" "}
            {backfill.result.estimate.credits_used_today}/{backfill.result.estimate.daily_credit_limit} credits used
          </div>
          <div style={{ fontSize: 12, color: A.muted, marginTop: 4 }}>{backfill.result.estimate.credits_note}</div>
          {!!backfill.result.would_refuse?.length && (
            <div style={{ fontSize: 12.5, color: A.red, marginTop: 6 }}>Refused: {backfill.result.would_refuse.join("; ")}</div>
          )}
          <div style={{ fontSize: 12, fontFamily: mono, color: A.body, marginTop: 6 }}>
            {backfill.result.items.slice(0, 20).map(i => `${i.name} (${i.status})`).join(" · ")}
          </div>
          <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
            {backfill.result.dry_run && !backfill.result.would_refuse?.length && backfill.result.items.length > 0 && (
              <Btn variant="primary" size="sm" disabled={busy}
                   onClick={() => { if (window.confirm("Queue the place_shape job for these names?")) runBackfill({ ...backfill.body, dry_run: false }); }}>
                Apply
              </Btn>
            )}
            <Btn size="sm" onClick={() => setBackfill(null)}>Close</Btn>
          </div>
        </Card>
      )}

      {manual && (
        <Card style={{ marginBottom: 16 }}>
          <SLabel>Manual line — {manual.item.name} ({manual.item.country})</SLabel>
          <div style={{ fontSize: 12, color: A.muted, marginBottom: 6 }}>
            A GeoJSON LineString / MultiLineString ([lng, lat] pairs). It is clipped to {manual.item.country}; the job never
            overwrites it. Use it only when no OpenStreetMap feature and no tour-day route fits.
          </div>
          <textarea aria-label="GeoJSON" value={manual.text} rows={6}
                    onChange={e => setManual({ ...manual, text: e.target.value })}
                    style={{ width: "100%", fontFamily: mono, fontSize: 12, border: `1px solid ${A.line}`, borderRadius: 8, padding: 8 }} />
          <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
            <Btn variant="primary" size="sm" disabled={busy || !manual.text.trim()} onClick={saveManual}>Save</Btn>
            <Btn size="sm" onClick={() => setManual(null)}>Cancel</Btn>
          </div>
        </Card>
      )}

      <DataTable<LineItem> tableId="admin-places-lines" data={data.items} columns={columns}
                           getRowId={it => it.destination_id} searchable searchPlaceholder="Line place"
                           enableCsv csvFilename="places-lines.csv"
                           emptyTitle="No line places" emptyDescription="No river, road or trail for these filters." />
    </>
  );
}
