"use client";
// app/admin/places/LinesTab.tsx — AA-847 (owner decision S246, D1): rivers, roads and trekking stretches are DRAWN
// on the TripPlanner map as lines — never a pin, a circle or hidden. This tab lists every line place and the state of
// its geometry (geo.place_shape): ok (an OpenStreetMap feature), approximate (no single OSM feature: the anchored
// route of a tour-day, drawn dashed "approximate route"), pending / not_found / failed (TripPlanner draws the
// provisional dashed line between the day's stops meanwhile), manual (an admin's GeoJSON — the exception path).
//   GET  /admin/places/lines?country=&status=
//   POST /admin/places/shapes/backfill   dry run first (names + Geoapify credit estimate), then apply (≤ 50 names)
//   PUT / DELETE /admin/places/{id}/shape  manual GeoJSON

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ColumnDef } from "@tanstack/react-table";
import { AlertTriangle, CheckCircle2, Route, Waves } from "lucide-react";
import {
  Button, DataTable, useFormat, useT, Badge, Eyebrow, K, KpiTile, LoadingScreen, Section, mono, type Tone,
} from "../../_kit";
import { PL } from "./messages";

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
const STATUS_COLOR: Record<string, Tone> = {
  ok: "success", manual: "info", approximate: "warning", pending: "neutral", missing: "danger", not_found: "danger", failed: "danger",
};
const selectStyle = { padding: "6px 8px", borderRadius: 8, border: `1px solid ${K.line}`, fontSize: 13 };
const linkBtn = { border: 0, background: "none", color: K.accentDeep, cursor: "pointer", padding: 0, fontSize: 12 };

export default function LinesTab({ countries, onError }: { countries: string[]; onError: (m: string) => void }) {
  const t = useT(PL);
  const { formatDateTime } = useFormat();
  // The loader reads the translator through a ref, so a language switch does not refetch.
  const tRef = useRef(t);
  useEffect(() => { tRef.current = t; }, [t]);
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
    if (!r.ok) { onError(tRef.current("linesFailed", r.status)); return; }
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
      if (!r.ok) { onError(t("backfillRefused", JSON.stringify(out.detail ?? r.status))); return; }
      setBackfill({ body, result: out });
      if (body.dry_run === false) await load();
    } finally { setBusy(false); }
  }

  async function saveManual() {
    if (!manual) return;
    let geojson: unknown;
    try { geojson = JSON.parse(manual.text); } catch { onError(t("badGeoJson")); return; }
    setBusy(true);
    try {
      const r = await fetch(`/api/admin/places/${manual.item.destination_id}/shape`, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ geojson }),
      });
      const out = await r.json().catch(() => ({}));
      if (!r.ok) { onError(t("manualRefused", JSON.stringify(out.detail ?? r.status))); return; }
      setManual(null);
      await load();
    } finally { setBusy(false); }
  }

  async function clearManual(item: LineItem) {
    if (!window.confirm(t("confirmClear", item.name))) return;
    const r = await fetch(`/api/admin/places/${item.destination_id}/shape`, { method: "DELETE" });
    if (!r.ok) { onError(t("clearFailed", r.status)); return; }
    await load();
  }

  const columns = useMemo<ColumnDef<LineItem, unknown>[]>(() => [
    { accessorKey: "name", header: t("linePlace") },
    { accessorKey: "country", header: t("country") },
    { accessorKey: "status", header: t("geometry"),
      cell: ({ row }) => <Badge tone={STATUS_COLOR[row.original.status] ?? "neutral"}>{row.original.status}</Badge> },
    { accessorKey: "source", header: t("source"), cell: ({ row }) => row.original.source ?? "—" },
    { accessorKey: "length_km", header: t("length"), meta: { align: "right" },
      cell: ({ row }) => row.original.length_km != null
        ? <span style={{ fontFamily: mono }}>{row.original.length_km.toFixed(1)} km</span> : "—" },
    { id: "osm", header: "OSM", accessorFn: it => it.osm_id ?? "",
      cell: ({ row }) => row.original.osm_id
        ? <a href={`https://www.openstreetmap.org/${row.original.osm_type}/${row.original.osm_id}`} target="_blank"
             rel="noreferrer" style={{ fontFamily: mono, fontSize: 12 }}>{row.original.osm_type}/{row.original.osm_id}</a>
        : "—" },
    { accessorKey: "tour_days", header: t("tourDays"), meta: { align: "right" } },
    { id: "retry", header: t("triesNext"), accessorFn: it => it.next_retry_at ?? "",
      cell: ({ row }) => <span style={{ fontSize: 12 }}>{row.original.tries ?? 0} · {formatDateTime(row.original.next_retry_at)}
        {row.original.last_error && <div style={{ color: K.muted }}>{row.original.last_error}</div>}</span> },
    { id: "act", header: "", accessorFn: () => "",
      cell: ({ row }) => {
        const it = row.original;
        return <span style={{ display: "flex", gap: 10 }}>
          {it.status !== "manual" && it.status !== "ok" && (
            <button disabled={busy} style={linkBtn}
                    onClick={() => runBackfill({ destination_ids: [it.destination_id], dry_run: true,
                                                 statuses: STATUSES.filter(s => s !== "manual") })}>{t("fetch")}</button>
          )}
          <button disabled={busy} style={linkBtn} onClick={() => setManual({ item: it, text: "" })}>{t("setGeoJson")}</button>
          {it.status === "manual" && <button disabled={busy} style={linkBtn} onClick={() => clearManual(it)}>{t("clearManual")}</button>}
        </span>;
      } },
  // eslint-disable-next-line react-hooks/exhaustive-deps -- the handlers only read state setters and load
  ], [busy, t, formatDateTime]);

  if (!data) return <LoadingScreen message={t("loadingLines")} />;
  const approx = Object.entries(data.approximate_by_country);
  const waiting = (data.totals.pending ?? 0) + (data.totals.missing ?? 0) + (data.totals.not_found ?? 0) + (data.totals.failed ?? 0);

  return (
    <>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 14, marginBottom: 18 }}>
        <KpiTile label={t("linePlaces")} value={String(data.lines)} icon={<Route size={16} />} hint={country || t("wholeCatalog")} />
        <KpiTile label={t("drawn")} value={`${data.drawn}/${data.lines}`} icon={<CheckCircle2 size={16} />}
                  hintTone={data.drawn === data.lines ? "success" : "warning"} hint="ok · manual · approximate" />
        <KpiTile label={t("approxRoutes")} value={String(data.totals.approximate ?? 0)} icon={<Waves size={16} />}
                  hintTone="warning" hint={approx.length ? approx.map(([c, n]) => `${c} ${n}`).join(" · ") : t("none")} />
        <KpiTile label={t("waitingShape")} value={String(waiting)} icon={<AlertTriangle size={16} />}
                  hintTone={waiting ? "warning" : "success"} hint={t("waitingSub")} />
        <KpiTile label={t("shapeJob")} value={data.enabled ? t("on") : t("off")} icon={<Route size={16} />}
                  hintTone={data.enabled ? "success" : "warning"} hint={data.enabled_env} />
      </div>

      <Section style={{ marginBottom: 16 }} bodyStyle={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center" }}>
        <select aria-label={t("country")} value={country} onChange={e => setCountry(e.target.value)} style={selectStyle}>
          <option value="">{t("allCountries")}</option>
          {countries.map(c => <option key={c} value={c}>{c}</option>)}
        </select>
        <select aria-label={t("geometryStatus")} value={status} onChange={e => setStatus(e.target.value)} style={selectStyle}>
          <option value="">{t("allStatuses")}</option>
          {STATUSES.map(s => <option key={s} value={s}>{s}</option>)}
        </select>
        <Button size="sm" disabled={busy} onClick={() => runBackfill({ country: country || null, limit: 50, dry_run: true })}>
          {t("previewBackfill", country ? ` (${country})` : "")}
        </Button>
      </Section>

      {backfill && (
        <Section style={{ marginBottom: 16 }}>
          <Eyebrow>{t("backfillTitle", backfill.result.dry_run ? t("dryRun") : t("queued"), backfill.result.provider)}</Eyebrow>
          <div style={{ fontSize: 13, color: K.body }}>
            {t("estimate", backfill.result.estimate.names, backfill.result.estimate.geoapify_credits_max,
              backfill.result.estimate.overpass_queries_max, backfill.result.estimate.credits_used_today,
              backfill.result.estimate.daily_credit_limit)}
          </div>
          <div style={{ fontSize: 12, color: K.muted, marginTop: 4 }}>{backfill.result.estimate.credits_note}</div>
          {!!backfill.result.would_refuse?.length && (
            <div style={{ fontSize: 12.5, color: K.danger, marginTop: 6 }}>{t("refused", backfill.result.would_refuse.join("; "))}</div>
          )}
          <div style={{ fontSize: 12, fontFamily: mono, color: K.body, marginTop: 6 }}>
            {backfill.result.items.slice(0, 20).map(i => `${i.name} (${i.status})`).join(" · ")}
          </div>
          <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
            {backfill.result.dry_run && !backfill.result.would_refuse?.length && backfill.result.items.length > 0 && (
              <Button variant="primary" size="sm" disabled={busy}
                   onClick={() => { if (window.confirm(t("confirmBackfill"))) runBackfill({ ...backfill.body, dry_run: false }); }}>
                {t("apply")}
              </Button>
            )}
            <Button size="sm" onClick={() => setBackfill(null)}>{t("close")}</Button>
          </div>
        </Section>
      )}

      {manual && (
        <Section style={{ marginBottom: 16 }}>
          <Eyebrow>{t("manualTitle", manual.item.name, manual.item.country)}</Eyebrow>
          <div style={{ fontSize: 12, color: K.muted, marginBottom: 6 }}>
            {t("manualHelp", manual.item.country)}
          </div>
          <textarea aria-label="GeoJSON" value={manual.text} rows={6}
                    onChange={e => setManual({ ...manual, text: e.target.value })}
                    style={{ width: "100%", fontFamily: mono, fontSize: 12, border: `1px solid ${K.line}`, borderRadius: 8, padding: 8 }} />
          <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
            <Button variant="primary" size="sm" disabled={busy || !manual.text.trim()} onClick={saveManual}>{t("save")}</Button>
            <Button size="sm" onClick={() => setManual(null)}>{t("cancel")}</Button>
          </div>
        </Section>
      )}

      <DataTable<LineItem> tableId="admin-places-lines" data={data.items} columns={columns}
                           getRowId={it => it.destination_id} searchable searchPlaceholder={t("linePlace")}
                           enableCsv csvFilename="places-lines.csv"
                           emptyTitle={t("noLines")} emptyDescription={t("noLinesDesc")} />
    </>
  );
}
