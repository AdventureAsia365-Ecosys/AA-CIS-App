"use client";
// app/admin/photos/page.tsx — AA-708 AA marketing photos for the whole system (Drive CON folders →
// S3 → shared.place_photo). Tabs: Overview (system KPIs + per-country coverage, incl. countries with
// no photos yet), Tours (photos per active tour, gaps first), Folders (each Drive folder, its tour,
// sample photos, folder-level assign), Gallery (every photo, any status). API: /admin/photos/*.
// Places (destinations) are matched only with "Match places + covers", per country, after its rerun
// tours are re-extracted; a photo is compared only with the places on its own tour's itinerary.

import { useCallback, useEffect, useRef, useState } from "react";
import { RefreshCw, HardDrive, Map as MapIcon, Images, AlertTriangle, CheckCircle2 } from "lucide-react";
import { A, sans, mono, Card, SLabel, Badge, LoadingScreen, TH, TD, StatCard } from "../_components/adminUi";
import { Button, Tabs, PageFrame, PageHeader, useFormat, useT } from "../../_kit";
import { PH } from "./messages";

type CountryRow = {
  country: string; photos: number; with_file: number; with_tour: number; with_destination: number;
  unmatched: number; rejected: number; errors: number; tour_folders: number; tours_covered: number;
  active_tours: number; destinations: number; destinations_with_cover: number; source_bytes: number;
  manual: number; last_synced: string | null; has_drive_folder: boolean;
};
type Totals = Record<"photos" | "with_file" | "with_tour" | "unmatched" | "rejected" | "errors" | "tours_covered" |
  "active_tours" | "source_bytes" | "destinations" | "destinations_with_cover" | "manual" | "tour_folders", number>;
type LastJob = { id: string; status: string; error: string | null; created_at: string | null; finished_at: string | null };
// AA-844: the daily scheduled sync (one job per country), latest UTC day it ran.
type ScheduledCountry = { country: string; status: string | null; errors: number | null; job_id: string | null; finished_at: string | null };
type LastScheduled = { day: string; countries: ScheduledCountry[]; all_ok: boolean };
type Photo = {
  id: string; country: string; folder_path: string; tour_folder: string | null; file_name: string;
  place_label: string | null; tour_id: string | null; tour_name: string | null; destination_name: string | null;
  match_source: string; status: string; width: number | null; height: number | null; error: string | null;
  url_small: string | null; url_large: string | null;
};
type Folder = {
  country: string; folder_path: string; tour_folder: string | null; photos: number; unmatched: number;
  errors: number; matched: number; tour_name: string | null; tour_id: string | null; manual: boolean; sample: string[];
};
type TourCov = { tour_id: string; src_name: string; country: string; duration: string | null; published: boolean; photos: number; sample: string[] };
type Option = { id: string; name: string };

const STATUS_COLOR: Record<string, "green" | "amber" | "gray" | "red"> = {
  matched: "green", unmatched: "amber", rejected: "gray", error: "red",
};
const PAGE = 120;
// Tab labels and photo status words are message keys (messages.ts); the status filter value stays the API code.
const TABS = [
  { key: "overview", label: "tabOverview" }, { key: "tours", label: "tabTours" },
  { key: "folders", label: "tabFolders" }, { key: "gallery", label: "tabGallery" },
] as const;
const STATUS_LABEL: Record<string, "statusMatched" | "statusUnmatched" | "statusRejected" | "statusError"> = {
  matched: "statusMatched", unmatched: "statusUnmatched", rejected: "statusRejected", error: "statusError",
};

const fmtMB = (b: number) => b >= 1e9 ? `${(b / 1e9).toFixed(2)} GB` : `${(b / 1e6).toFixed(1)} MB`;
const pct = (a: number, b: number) => b ? Math.round((a / b) * 100) : 0;

function Bar({ value, total, color = A.accent }: { value: number; total: number; color?: string }) {
  const p = pct(value, total);
  return (
    <div style={{ minWidth: 120 }}>
      <div style={{ fontSize: 11.5, fontFamily: mono }}>{value}/{total} · {p}%</div>
      <div style={{ height: 5, background: A.line2, borderRadius: 3, overflow: "hidden", marginTop: 3 }}>
        <div style={{ width: `${p}%`, height: "100%", background: color }} />
      </div>
    </div>
  );
}

function Thumbs({ urls, h = 56 }: { urls: string[]; h?: number }) {
  const t = useT(PH);
  if (!urls.length) return <span style={{ fontSize: 12, color: A.muted }}>{t("noPhoto")}</span>;
  return (
    <div style={{ display: "flex", gap: 4 }}>
      {urls.map(u => (
        // eslint-disable-next-line @next/next/no-img-element -- public redirect URL, small thumbnails
        <img key={u} src={u} alt="" loading="lazy" style={{ width: h * 1.4, height: h, objectFit: "cover", borderRadius: 6, background: A.line2 }} />
      ))}
    </div>
  );
}

const selectStyle = { padding: "6px 8px", borderRadius: 8, border: `1px solid ${A.line}`, fontFamily: sans, fontSize: 13 };

export default function PhotosPage() {
  const t = useT(PH);
  const { formatDateTime: fmtTime } = useFormat();
  const statusLabel = (s: string) => (STATUS_LABEL[s] ? t(STATUS_LABEL[s]) : s);
  // The loaders read the translator through a ref, so a language switch does not refetch.
  const tRef = useRef(t);
  useEffect(() => { tRef.current = t; }, [t]);
  const [tab, setTab] = useState("overview");
  const [countries, setCountries] = useState<CountryRow[] | null>(null);
  const [totals, setTotals] = useState<Totals | null>(null);
  const [lastJob, setLastJob] = useState<LastJob | null>(null);
  const [lastScheduled, setLastScheduled] = useState<LastScheduled | null>(null);
  const [country, setCountry] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [matchPlaces, setMatchPlaces] = useState(false);
  // Tours tab
  const [tours, setTours] = useState<TourCov[]>([]);
  const [onlyMissing, setOnlyMissing] = useState(false);
  // Folders tab
  const [folders, setFolders] = useState<Folder[]>([]);
  const [folderFilter, setFolderFilter] = useState("all");
  const [assignFolder, setAssignFolder] = useState<Folder | null>(null);
  const [tourOpts, setTourOpts] = useState<Option[]>([]);
  const [pickTour, setPickTour] = useState("");
  // Gallery tab
  const [photos, setPhotos] = useState<Photo[]>([]);
  const [total, setTotal] = useState(0);
  const [status, setStatus] = useState("");
  const [offset, setOffset] = useState(0);

  const loadSummary = useCallback(async () => {
    const r = await fetch("/api/admin/photos/summary");
    if (!r.ok) { setError(tRef.current("summaryFailed", r.status)); return; }
    const body = await r.json();
    setCountries(body.countries); setTotals(body.totals); setLastJob(body.last_job);
    setLastScheduled(body.last_scheduled ?? null);
  }, []);

  const loadTours = useCallback(async () => {
    const qs = new URLSearchParams();
    if (country) qs.set("country", country);
    if (onlyMissing) qs.set("only_missing", "true");
    const r = await fetch(`/api/admin/photos/tour-coverage?${qs}`);
    setTours(r.ok ? (await r.json()).tours : []);
  }, [country, onlyMissing]);

  const loadFolders = useCallback(async () => {
    const r = await fetch(`/api/admin/photos/folders${country ? `?country=${encodeURIComponent(country)}` : ""}`);
    setFolders(r.ok ? (await r.json()).folders : []);
  }, [country]);

  const loadPhotos = useCallback(async () => {
    const qs = new URLSearchParams({ limit: String(PAGE), offset: String(offset) });
    if (country) qs.set("country", country);
    if (status) qs.set("status", status);
    const r = await fetch(`/api/admin/photos?${qs}`);
    if (!r.ok) { setError(tRef.current("photosFailed", r.status)); return; }
    const body = await r.json();
    setPhotos(body.photos); setTotal(body.total);
  }, [country, status, offset]);

  /* eslint-disable react-hooks/set-state-in-effect -- fetch on mount / tab / filter change, same pattern as every admin page */
  useEffect(() => { loadSummary(); }, [loadSummary]);
  useEffect(() => { if (tab === "tours") loadTours(); }, [tab, loadTours]);
  useEffect(() => { if (tab === "folders") loadFolders(); }, [tab, loadFolders]);
  useEffect(() => { if (tab === "gallery") loadPhotos(); }, [tab, loadPhotos]);
  /* eslint-enable react-hooks/set-state-in-effect */

  function refresh() {
    loadSummary();
    if (tab === "tours") loadTours();
    if (tab === "folders") loadFolders();
    if (tab === "gallery") loadPhotos();
  }

  async function syncNow() {
    const scope = country || t("allConFolders");
    const extra = matchPlaces ? t("matchExtra") : "";
    if (!window.confirm(t("confirmSync", scope, extra))) return;
    setBusy("sync");
    try {
      const r = await fetch("/api/admin/photos/sync", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ countries: country ? [country] : null, match_destinations: matchPlaces }),
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) setError(t("syncFailed", String(body.detail ?? r.status)));
      await loadSummary();
    } finally { setBusy(null); }
  }

  async function openAssign(f: Folder) {
    setAssignFolder(f);
    setPickTour(f.tour_id ?? "");
    const r = await fetch(`/api/admin/photos/tours?country=${encodeURIComponent(f.country)}`);
    setTourOpts(r.ok ? (await r.json()).tours : []);
  }

  async function assignFolderTo(f: Folder) {
    // Folder-level assign: the API applies it to every photo in the folder (whole_folder).
    const first = await fetch(`/api/admin/photos?country=${encodeURIComponent(f.country)}&folder_path=${encodeURIComponent(f.folder_path)}&limit=1`);
    const p = first.ok ? (await first.json()).photos[0] : null;
    if (!p) { setError(t("folderNoPhoto")); return; }
    setBusy(f.folder_path);
    try {
      const r = await fetch(`/api/admin/photos/${p.id}/assign`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tour_id: pickTour, whole_folder: true }),
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) { setError(t("assignFailed", String(body.detail ?? r.status))); return; }
      setAssignFolder(null);
      await Promise.all([loadFolders(), loadSummary()]);
    } finally { setBusy(null); }
  }

  async function reject(p: Photo) {
    setBusy(p.id);
    try {
      const r = await fetch(`/api/admin/photos/${p.id}/assign`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ reject: true }),
      });
      if (!r.ok) setError(t("rejectFailed", r.status));
      await Promise.all([loadPhotos(), loadSummary()]);
    } finally { setBusy(null); }
  }

  if (!countries || !totals) return <PageFrame><LoadingScreen msg={t("loading")} /></PageFrame>;
  const jobRunning = lastJob && ["queued", "running"].includes(lastJob.status);
  const shownFolders = folders.filter(f => folderFilter === "all" || (folderFilter === "unmatched" ? !f.tour_id : !!f.tour_id));

  return (
      <PageFrame>
        <PageHeader
          title={t("title")}
          description={t("description")}
          actions={
            <>
              <select value={country} onChange={e => { setCountry(e.target.value); setOffset(0); }} style={selectStyle}>
                <option value="">{t("allCountries")}</option>
                {countries.map(c => <option key={c.country} value={c.country}>{c.country}</option>)}
              </select>
              <label style={{ fontSize: 12.5, color: A.body, display: "flex", gap: 6, alignItems: "center" }}
                     title={t("matchTitle")}>
                <input type="checkbox" checked={matchPlaces} onChange={e => setMatchPlaces(e.target.checked)} />
                {t("matchPlaces")}
              </label>
              <Button onClick={refresh} size="sm"><RefreshCw size={13} /> {t("refresh")}</Button>
              <Button onClick={syncNow} variant="primary" size="sm" disabled={busy === "sync" || !!jobRunning}>
                {jobRunning ? t("syncRunning") : country ? t("syncCountry", country) : t("syncAll")}
              </Button>
            </>
          }
        />

        {error && (
          <Card style={{ marginBottom: 16, borderColor: A.redBorder, background: A.redTint }}>
            <span style={{ color: A.red, fontSize: 13 }}>{error}</span>{" "}
            <button onClick={() => setError(null)} style={{ border: 0, background: "none", cursor: "pointer", color: A.muted }}>{t("dismiss")}</button>
          </Card>
        )}

        <div style={{ marginBottom: 18 }}><Tabs tabs={TABS.map((x) => ({ key: x.key, label: t(x.label) }))} active={tab} onChange={setTab} /></div>

        {tab === "overview" && (
          <>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(190px, 1fr))", gap: 14, marginBottom: 18 }}>
              <StatCard label={t("photosSynced")} value={String(totals.photos)} icon={<Images size={16} />}
                        sub={t("syncedSub", totals.with_file, totals.tour_folders)} />
              <StatCard label={t("toursWithPhotos")} value={`${pct(totals.tours_covered, totals.active_tours)}%`} icon={<CheckCircle2 size={16} />}
                        accent={A.green} sub={t("toursSub", totals.tours_covered, totals.active_tours)} />
              <StatCard label={t("matchedToTour")} value={String(totals.with_tour)} icon={<MapIcon size={16} />}
                        sub={t("matchedSub", totals.manual, totals.unmatched)} />
              <StatCard label={t("placeCovers")} value={`${totals.destinations_with_cover}`} icon={<MapIcon size={16} />}
                        sub={t("coversSub", totals.destinations)} />
              <StatCard label={t("errors")} value={String(totals.errors)} icon={<AlertTriangle size={16} />}
                        accent={totals.errors ? A.red : A.accent} sub={t("rejectedSub", totals.rejected)} />
              <StatCard label={t("sourceSize")} value={fmtMB(totals.source_bytes)} icon={<HardDrive size={16} />}
                        sub={t("sizeSub")} />
            </div>

            <Card style={{ marginBottom: 18 }}>
              <SLabel>{t("dailySync")}</SLabel>
              {lastScheduled ? (
                <div style={{ fontSize: 13, color: A.body }}>
                  <div style={{ marginBottom: 8 }}>
                    {lastScheduled.day} {t("utc")} ·{" "}
                    <Badge color={lastScheduled.all_ok ? "green" : "amber"}>{lastScheduled.all_ok ? t("allClean") : t("needsLook")}</Badge>
                  </div>
                  <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                    {lastScheduled.countries.map(c => (
                      <span key={c.country} style={{ display: "inline-flex", alignItems: "center", gap: 6, padding: "4px 8px",
                                                     border: `1px solid ${A.line}`, borderRadius: 8, maxWidth: "100%" }}>
                        {c.country}
                        <Badge color={c.status === "succeeded" ? (c.errors ? "amber" : "green") : c.status === "failed" ? "red" : c.status ? "amber" : "gray"}>
                          {c.status ?? t("notRun")}
                        </Badge>
                        {c.errors != null && <span style={{ fontFamily: mono, fontSize: 12 }}>{t("errShort", c.errors)}</span>}
                        {c.job_id && <a href={`/admin/jobs?job=${c.job_id}`} style={{ color: A.accentDeep, fontSize: 12 }}>{t("job")}</a>}
                      </span>
                    ))}
                  </div>
                </div>
              ) : <div style={{ fontSize: 13, color: A.muted }}>{t("noScheduled")}</div>}
            </Card>

            <Card style={{ marginBottom: 18 }}>
              <SLabel>{t("lastJob")}</SLabel>
              {lastJob ? (
                <div style={{ fontSize: 13, color: A.body }}>
                  <Badge color={lastJob.status === "succeeded" ? "green" : lastJob.status === "failed" ? "red" : "amber"}>{lastJob.status}</Badge>{" "}
                  {t("jobTimes", fmtTime(lastJob.created_at), fmtTime(lastJob.finished_at))}{" "}
                  <a href={`/admin/jobs?job=${lastJob.id}`} style={{ color: A.accentDeep }}>{t("jobId", lastJob.id.slice(0, 8))}</a>
                  {lastJob.error && <div style={{ color: A.red, marginTop: 6, fontFamily: mono, fontSize: 12 }}>{lastJob.error}</div>}
                </div>
              ) : <div style={{ fontSize: 13, color: A.muted }}>{t("noSync")}</div>}
            </Card>

            <Card style={{ padding: 0, overflowX: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "collapse" }}>
                <thead>
                  <tr>{([
                    "colCountry", "colDriveFolder", "colPhotos", "colInS3", "colToursWithPhotos", "colMatched",
                    "colUnmatched", "colErrors", "colPlaceCovers", "colSize", "colLastSynced",
                  ] as const).map(h => <th key={h} style={TH}>{t(h)}</th>)}</tr>
                </thead>
                <tbody>
                  {countries.map(c => (
                    <tr key={c.country} onClick={() => { setCountry(c.country); setTab("tours"); }} style={{ cursor: "pointer", background: c.country === country ? A.accentTint : undefined }}>
                      <td style={{ ...TD, fontWeight: 600 }}>{c.country}</td>
                      <td style={TD}>{c.has_drive_folder ? <Badge color="green">{t("linked")}</Badge> : <Badge color="gray">{t("none")}</Badge>}</td>
                      <td style={{ ...TD, fontFamily: mono }}>{c.photos}</td>
                      <td style={{ ...TD, fontFamily: mono }}>{c.with_file}</td>
                      <td style={TD}><Bar value={c.tours_covered} total={c.active_tours} color={A.green} /></td>
                      <td style={{ ...TD, fontFamily: mono }}>{c.with_tour}</td>
                      <td style={{ ...TD, fontFamily: mono, color: c.unmatched ? A.amber : undefined }}>{c.unmatched}</td>
                      <td style={{ ...TD, fontFamily: mono, color: c.errors ? A.red : undefined }}>{c.errors}</td>
                      <td style={{ ...TD, fontFamily: mono }}>{c.destinations_with_cover}/{c.destinations}</td>
                      <td style={{ ...TD, fontFamily: mono }}>{c.source_bytes ? fmtMB(c.source_bytes) : "—"}</td>
                      <td style={TD}>{fmtTime(c.last_synced)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Card>
          </>
        )}

        {tab === "tours" && (
          <>
            <div style={{ display: "flex", gap: 12, alignItems: "center", marginBottom: 12 }}>
              <label style={{ fontSize: 13, display: "flex", gap: 6, alignItems: "center" }}>
                <input type="checkbox" checked={onlyMissing} onChange={e => setOnlyMissing(e.target.checked)} /> {t("onlyMissing")}
              </label>
              <span style={{ fontSize: 12.5, color: A.muted }}>
                {t("toursHavePhotos", tours.filter(x => x.photos).length, tours.length, country ? t("inCountry", country) : "")}
              </span>
            </div>
            <Card style={{ padding: 0, overflowX: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "collapse" }}>
                <thead><tr>{(["colTour", "colCountry", "colDuration", "colPublished", "colPhotos", "colSample"] as const)
                  .map(h => <th key={h} style={TH}>{t(h)}</th>)}</tr></thead>
                <tbody>
                  {tours.map(tc => (
                    <tr key={tc.tour_id}>
                      <td style={{ ...TD, fontWeight: 600, maxWidth: 360 }}>{tc.src_name}</td>
                      <td style={TD}>{tc.country}</td>
                      <td style={TD}>{tc.duration ?? "—"}</td>
                      <td style={TD}>{tc.published ? <Badge color="green">{t("yes")}</Badge> : <Badge color="gray">{t("no")}</Badge>}</td>
                      <td style={{ ...TD, fontFamily: mono, color: tc.photos ? undefined : A.amber }}>{tc.photos}</td>
                      <td style={TD}><Thumbs urls={tc.sample} h={40} /></td>
                    </tr>
                  ))}
                  {tours.length === 0 && <tr><td style={TD} colSpan={6}>{t("noTours")}</td></tr>}
                </tbody>
              </table>
            </Card>
          </>
        )}

        {tab === "folders" && (
          <>
            <div style={{ display: "flex", gap: 10, alignItems: "center", marginBottom: 12 }}>
              <select value={folderFilter} onChange={e => setFolderFilter(e.target.value)} style={selectStyle}>
                <option value="all">{t("allFolders", folders.length)}</option>
                <option value="unmatched">{t("notMatchedFolders", folders.filter(f => !f.tour_id).length)}</option>
                <option value="matched">{t("matchedFolders", folders.filter(f => !!f.tour_id).length)}</option>
              </select>
              <span style={{ fontSize: 12.5, color: A.muted }}>{t("assignHint")}</span>
            </div>
            <div style={{ display: "grid", gap: 12 }}>
              {shownFolders.map(f => (
                <Card key={`${f.country}|${f.folder_path}`} style={{ padding: "14px 16px" }}>
                  <div style={{ display: "flex", gap: 16, alignItems: "center", flexWrap: "wrap" }}>
                    <Thumbs urls={f.sample} />
                    <div style={{ flex: 1, minWidth: 260 }}>
                      <div style={{ fontWeight: 600, color: A.ink, fontSize: 13.5 }}>{f.folder_path || t("countryRoot")}</div>
                      <div style={{ fontSize: 12.5, color: A.muted }}>
                        {t("folderLine", f.country, f.photos, f.errors ? t("folderErrors", f.errors) : "")}
                      </div>
                      <div style={{ fontSize: 12.5, marginTop: 4 }}>
                        {f.tour_id
                          ? <>{t("tourLabel")} <b>{f.tour_name}</b> {f.manual && <Badge color="blue">{t("manual")}</Badge>}</>
                          : <Badge color="amber">{t("noTour")}</Badge>}
                      </div>
                    </div>
                    {assignFolder && assignFolder.folder_path === f.folder_path && assignFolder.country === f.country ? (
                      <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
                        <select value={pickTour} onChange={e => setPickTour(e.target.value)} style={{ ...selectStyle, maxWidth: 320 }}>
                          <option value="">{t("chooseTour")}</option>
                          {tourOpts.map(o => <option key={o.id} value={o.id}>{o.name}</option>)}
                        </select>
                        <Button size="sm" variant="primary" disabled={!pickTour || busy === f.folder_path} onClick={() => assignFolderTo(f)}>{t("save")}</Button>
                        <Button size="sm" onClick={() => setAssignFolder(null)}>{t("cancel")}</Button>
                      </div>
                    ) : (
                      <Button size="sm" onClick={() => openAssign(f)}>{f.tour_id ? t("changeTour") : t("assignTour")}</Button>
                    )}
                  </div>
                </Card>
              ))}
              {shownFolders.length === 0 && <div style={{ color: A.muted, fontSize: 13 }}>{t("noFolders")}</div>}
            </div>
          </>
        )}

        {tab === "gallery" && (
          <>
            <div style={{ display: "flex", gap: 10, alignItems: "center", marginBottom: 12, flexWrap: "wrap" }}>
              <select value={status} onChange={e => { setStatus(e.target.value); setOffset(0); }} style={selectStyle}>
                <option value="">{t("allStatuses")}</option>
                {Object.keys(STATUS_COLOR).map(s => <option key={s} value={s}>{statusLabel(s)}</option>)}
              </select>
              <span style={{ fontSize: 12.5, color: A.muted }}>{t("photosCount", total)}</span>
              <div style={{ flex: 1 }} />
              <Button size="sm" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>{t("prev")}</Button>
              <Button size="sm" disabled={offset + PAGE >= total} onClick={() => setOffset(offset + PAGE)}>{t("next")}</Button>
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(200px, 1fr))", gap: 14 }}>
              {photos.map(p => (
                <Card key={p.id} style={{ padding: 0, overflow: "hidden" }}>
                  {p.url_small
                    // eslint-disable-next-line @next/next/no-img-element -- public redirect URL, sized thumbnails
                    ? <a href={p.url_large ?? undefined} target="_blank" rel="noreferrer"><img src={p.url_small} alt={p.place_label ?? p.file_name} loading="lazy" style={{ width: "100%", height: 135, objectFit: "cover", display: "block", background: A.line2 }} /></a>
                    : <div style={{ height: 135, display: "grid", placeItems: "center", background: A.line2, color: A.muted, fontSize: 12 }}>{t("notStored")}</div>}
                  <div style={{ padding: "10px 12px", fontSize: 12.5 }}>
                    <div style={{ display: "flex", gap: 6, alignItems: "center", marginBottom: 4 }}>
                      <Badge color={STATUS_COLOR[p.status] ?? "gray"}>{statusLabel(p.status)}</Badge>
                      {p.match_source === "manual" && <Badge color="blue">{t("manual")}</Badge>}
                      {p.width && <span style={{ color: A.muted, fontFamily: mono, fontSize: 11 }}>{p.width}×{p.height}</span>}
                    </div>
                    <div style={{ fontWeight: 600, color: A.ink }} title={p.file_name}>{p.place_label || p.file_name}</div>
                    <div style={{ color: A.muted }} title={p.folder_path}>{p.country} · {p.tour_folder ?? t("countryRoot")}</div>
                    <div style={{ color: A.body, marginTop: 4 }}>{t("tourValue", p.tour_name ?? "—")}</div>
                    {p.destination_name && <div style={{ color: A.body }}>{t("placeValue", p.destination_name)}</div>}
                    {p.error && <div style={{ color: A.red, fontFamily: mono, fontSize: 11, marginTop: 4 }}>{p.error}</div>}
                    {p.status !== "rejected" && (
                      <div style={{ marginTop: 8 }}>
                        <Button size="sm" variant="ghost" disabled={busy === p.id} onClick={() => reject(p)}>{t("reject")}</Button>
                      </div>
                    )}
                  </div>
                </Card>
              ))}
            </div>
            {photos.length === 0 && <div style={{ color: A.muted, fontSize: 13 }}>{t("noPhotos")}</div>}
          </>
        )}
      </PageFrame>
  );
}
