"use client";
// app/admin/photos/page.tsx — AA-708 AA marketing photos for the whole system (Drive CON folders →
// S3 → shared.place_photo). Tabs: Overview (system KPIs + per-country coverage, incl. countries with
// no photos yet), Tours (photos per active tour, gaps first), Folders (each Drive folder, its tour,
// sample photos, folder-level assign), Gallery (every photo, any status). API: /admin/photos/*.
// Places (destinations) are matched only with "Match places + covers", per country, after its rerun
// tours are re-extracted; a photo is compared only with the places on its own tour's itinerary.

import { useCallback, useEffect, useState } from "react";
import { Image as ImageIcon, RefreshCw, HardDrive, Map as MapIcon, Images, AlertTriangle, CheckCircle2 } from "lucide-react";
import { A, alpha, serif, sans, mono, Card, SLabel, Badge, Btn, LoadingScreen, TH, TD, StatCard, TabBar } from "../_components/adminUi";

type CountryRow = {
  country: string; photos: number; with_file: number; with_tour: number; with_destination: number;
  unmatched: number; rejected: number; errors: number; tour_folders: number; tours_covered: number;
  active_tours: number; destinations: number; destinations_with_cover: number; source_bytes: number;
  manual: number; last_synced: string | null; has_drive_folder: boolean;
};
type Totals = Record<"photos" | "with_file" | "with_tour" | "unmatched" | "rejected" | "errors" | "tours_covered" |
  "active_tours" | "source_bytes" | "destinations" | "destinations_with_cover" | "manual" | "tour_folders", number>;
type LastJob = { id: string; status: string; error: string | null; created_at: string | null; finished_at: string | null };
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
const TABS = [
  { key: "overview", label: "Overview" }, { key: "tours", label: "Tours" },
  { key: "folders", label: "Folders" }, { key: "gallery", label: "Gallery" },
];

const fmtTime = (s: string | null) => s ? new Date(s).toLocaleString("en-GB", { dateStyle: "short", timeStyle: "short" }) : "—";
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
  if (!urls.length) return <span style={{ fontSize: 12, color: A.muted }}>no photo</span>;
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
  const [tab, setTab] = useState("overview");
  const [countries, setCountries] = useState<CountryRow[] | null>(null);
  const [totals, setTotals] = useState<Totals | null>(null);
  const [lastJob, setLastJob] = useState<LastJob | null>(null);
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
    if (!r.ok) { setError(`Summary failed (${r.status})`); return; }
    const body = await r.json();
    setCountries(body.countries); setTotals(body.totals); setLastJob(body.last_job);
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
    if (!r.ok) { setError(`Photos failed (${r.status})`); return; }
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
    const scope = country || "all CON photo folders";
    const extra = matchPlaces ? " and match photos to places on each tour's itinerary (sets TripPlanner covers)" : "";
    if (!window.confirm(`Sync ${scope} from Google Drive${extra}?`)) return;
    setBusy("sync");
    try {
      const r = await fetch("/api/admin/photos/sync", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ countries: country ? [country] : null, match_destinations: matchPlaces }),
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) setError(`Sync failed: ${body.detail ?? r.status}`);
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
    if (!p) { setError("Folder has no photo"); return; }
    setBusy(f.folder_path);
    try {
      const r = await fetch(`/api/admin/photos/${p.id}/assign`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tour_id: pickTour, whole_folder: true }),
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) { setError(`Assign failed: ${body.detail ?? r.status}`); return; }
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
      if (!r.ok) setError(`Reject failed (${r.status})`);
      await Promise.all([loadPhotos(), loadSummary()]);
    } finally { setBusy(null); }
  }

  if (!countries || !totals) return <LoadingScreen msg="Loading photos..." />;
  const jobRunning = lastJob && ["queued", "running"].includes(lastJob.status);
  const shownFolders = folders.filter(f => folderFilter === "all" || (folderFilter === "unmatched" ? !f.tour_id : !!f.tour_id));

  return (
      <main className="aa-admin-main" style={{ flex: 1, padding: "32px 36px", minWidth: 0, minHeight: 0, overflowY: "auto" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 18, flexWrap: "wrap" }}>
          <div style={{ width: 36, height: 36, borderRadius: 9, background: alpha(A.accent, 8), color: A.accent, display: "grid", placeItems: "center" }}>
            <ImageIcon size={18} />
          </div>
          <div style={{ flex: 1, minWidth: 240 }}>
            <h1 style={{ fontFamily: serif, fontSize: 24, margin: 0, color: A.ink }}>Photos</h1>
            <div style={{ fontSize: 12.5, color: A.muted }}>
              AA marketing photos for the whole system — Contents board (Google Drive) → S3, matched to tours and places.
              Read by the TripPlanner, portal and AA-Booking through the photo views.
            </div>
          </div>
          <select value={country} onChange={e => { setCountry(e.target.value); setOffset(0); }} style={selectStyle}>
            <option value="">All countries</option>
            {countries.map(c => <option key={c.country} value={c.country}>{c.country}</option>)}
          </select>
          <label style={{ fontSize: 12.5, color: A.body, display: "flex", gap: 6, alignItems: "center" }}
                 title="Only after this country's tours are rewritten, atomized and their places re-extracted">
            <input type="checkbox" checked={matchPlaces} onChange={e => setMatchPlaces(e.target.checked)} />
            Match places + covers
          </label>
          <Btn onClick={refresh} size="sm"><RefreshCw size={13} /> Refresh</Btn>
          <Btn onClick={syncNow} variant="primary" size="sm" disabled={busy === "sync" || !!jobRunning}>
            {jobRunning ? "Sync running…" : country ? `Sync ${country}` : "Sync all"}
          </Btn>
        </div>

        {error && (
          <Card style={{ marginBottom: 16, borderColor: A.redBorder, background: A.redTint }}>
            <span style={{ color: A.red, fontSize: 13 }}>{error}</span>{" "}
            <button onClick={() => setError(null)} style={{ border: 0, background: "none", cursor: "pointer", color: A.muted }}>dismiss</button>
          </Card>
        )}

        <div style={{ marginBottom: 18 }}><TabBar tabs={TABS} active={tab} onChange={setTab} /></div>

        {tab === "overview" && (
          <>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(190px, 1fr))", gap: 14, marginBottom: 18 }}>
              <StatCard label="Photos synced" value={String(totals.photos)} icon={<Images size={16} />}
                        sub={`${totals.with_file} stored in S3 · ${totals.tour_folders} Drive folders`} />
              <StatCard label="Tours with photos" value={`${pct(totals.tours_covered, totals.active_tours)}%`} icon={<CheckCircle2 size={16} />}
                        accent={A.green} sub={`${totals.tours_covered} of ${totals.active_tours} active tours`} />
              <StatCard label="Matched to a tour" value={String(totals.with_tour)} icon={<MapIcon size={16} />}
                        sub={`${totals.manual} assigned by hand · ${totals.unmatched} unmatched`} />
              <StatCard label="Place covers" value={`${totals.destinations_with_cover}`} icon={<MapIcon size={16} />}
                        sub={`of ${totals.destinations} places (after re-extraction)`} />
              <StatCard label="Errors" value={String(totals.errors)} icon={<AlertTriangle size={16} />}
                        accent={totals.errors ? A.red : A.accent} sub={`${totals.rejected} rejected by hand`} />
              <StatCard label="Source size" value={fmtMB(totals.source_bytes)} icon={<HardDrive size={16} />}
                        sub="stored as 1600w + 600w WebP" />
            </div>

            <Card style={{ marginBottom: 18 }}>
              <SLabel>Last sync</SLabel>
              {lastJob ? (
                <div style={{ fontSize: 13, color: A.body }}>
                  <Badge color={lastJob.status === "succeeded" ? "green" : lastJob.status === "failed" ? "red" : "amber"}>{lastJob.status}</Badge>{" "}
                  started {fmtTime(lastJob.created_at)} · finished {fmtTime(lastJob.finished_at)} ·{" "}
                  <a href={`/admin/jobs?job=${lastJob.id}`} style={{ color: A.accentDeep }}>job {lastJob.id.slice(0, 8)}</a>
                  {lastJob.error && <div style={{ color: A.red, marginTop: 6, fontFamily: mono, fontSize: 12 }}>{lastJob.error}</div>}
                </div>
              ) : <div style={{ fontSize: 13, color: A.muted }}>No sync yet.</div>}
            </Card>

            <Card style={{ padding: 0, overflowX: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "collapse" }}>
                <thead>
                  <tr>{["Country", "Drive folder", "Photos", "In S3", "Tours with photos", "Matched", "Unmatched", "Errors", "Place covers", "Size", "Last synced"].map(h => <th key={h} style={TH}>{h}</th>)}</tr>
                </thead>
                <tbody>
                  {countries.map(c => (
                    <tr key={c.country} onClick={() => { setCountry(c.country); setTab("tours"); }} style={{ cursor: "pointer", background: c.country === country ? A.accentTint : undefined }}>
                      <td style={{ ...TD, fontWeight: 600 }}>{c.country}</td>
                      <td style={TD}>{c.has_drive_folder ? <Badge color="green">linked</Badge> : <Badge color="gray">none</Badge>}</td>
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
                <input type="checkbox" checked={onlyMissing} onChange={e => setOnlyMissing(e.target.checked)} /> Only tours without photos
              </label>
              <span style={{ fontSize: 12.5, color: A.muted }}>
                {tours.filter(t => t.photos).length} of {tours.length} tours have photos{country ? ` in ${country}` : ""}
              </span>
            </div>
            <Card style={{ padding: 0, overflowX: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "collapse" }}>
                <thead><tr>{["Tour", "Country", "Duration", "Published", "Photos", "Sample"].map(h => <th key={h} style={TH}>{h}</th>)}</tr></thead>
                <tbody>
                  {tours.map(t => (
                    <tr key={t.tour_id}>
                      <td style={{ ...TD, fontWeight: 600, maxWidth: 360 }}>{t.src_name}</td>
                      <td style={TD}>{t.country}</td>
                      <td style={TD}>{t.duration ?? "—"}</td>
                      <td style={TD}>{t.published ? <Badge color="green">yes</Badge> : <Badge color="gray">no</Badge>}</td>
                      <td style={{ ...TD, fontFamily: mono, color: t.photos ? undefined : A.amber }}>{t.photos}</td>
                      <td style={TD}><Thumbs urls={t.sample} h={40} /></td>
                    </tr>
                  ))}
                  {tours.length === 0 && <tr><td style={TD} colSpan={6}>No tours for this filter.</td></tr>}
                </tbody>
              </table>
            </Card>
          </>
        )}

        {tab === "folders" && (
          <>
            <div style={{ display: "flex", gap: 10, alignItems: "center", marginBottom: 12 }}>
              <select value={folderFilter} onChange={e => setFolderFilter(e.target.value)} style={selectStyle}>
                <option value="all">All folders ({folders.length})</option>
                <option value="unmatched">Not matched to a tour ({folders.filter(f => !f.tour_id).length})</option>
                <option value="matched">Matched ({folders.filter(f => !!f.tour_id).length})</option>
              </select>
              <span style={{ fontSize: 12.5, color: A.muted }}>Assigning a tour applies to every photo in the folder.</span>
            </div>
            <div style={{ display: "grid", gap: 12 }}>
              {shownFolders.map(f => (
                <Card key={`${f.country}|${f.folder_path}`} style={{ padding: "14px 16px" }}>
                  <div style={{ display: "flex", gap: 16, alignItems: "center", flexWrap: "wrap" }}>
                    <Thumbs urls={f.sample} />
                    <div style={{ flex: 1, minWidth: 260 }}>
                      <div style={{ fontWeight: 600, color: A.ink, fontSize: 13.5 }}>{f.folder_path || "(country root)"}</div>
                      <div style={{ fontSize: 12.5, color: A.muted }}>
                        {f.country} · {f.photos} photos{f.errors ? ` · ${f.errors} errors` : ""}
                      </div>
                      <div style={{ fontSize: 12.5, marginTop: 4 }}>
                        {f.tour_id
                          ? <>Tour: <b>{f.tour_name}</b> {f.manual && <Badge color="blue">manual</Badge>}</>
                          : <Badge color="amber">no tour</Badge>}
                      </div>
                    </div>
                    {assignFolder && assignFolder.folder_path === f.folder_path && assignFolder.country === f.country ? (
                      <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
                        <select value={pickTour} onChange={e => setPickTour(e.target.value)} style={{ ...selectStyle, maxWidth: 320 }}>
                          <option value="">Choose a tour…</option>
                          {tourOpts.map(t => <option key={t.id} value={t.id}>{t.name}</option>)}
                        </select>
                        <Btn size="sm" variant="primary" disabled={!pickTour || busy === f.folder_path} onClick={() => assignFolderTo(f)}>Save</Btn>
                        <Btn size="sm" onClick={() => setAssignFolder(null)}>Cancel</Btn>
                      </div>
                    ) : (
                      <Btn size="sm" onClick={() => openAssign(f)}>{f.tour_id ? "Change tour" : "Assign tour"}</Btn>
                    )}
                  </div>
                </Card>
              ))}
              {shownFolders.length === 0 && <div style={{ color: A.muted, fontSize: 13 }}>No folders for this filter.</div>}
            </div>
          </>
        )}

        {tab === "gallery" && (
          <>
            <div style={{ display: "flex", gap: 10, alignItems: "center", marginBottom: 12, flexWrap: "wrap" }}>
              <select value={status} onChange={e => { setStatus(e.target.value); setOffset(0); }} style={selectStyle}>
                <option value="">All statuses</option>
                {Object.keys(STATUS_COLOR).map(s => <option key={s} value={s}>{s}</option>)}
              </select>
              <span style={{ fontSize: 12.5, color: A.muted }}>{total} photos</span>
              <div style={{ flex: 1 }} />
              <Btn size="sm" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>Prev</Btn>
              <Btn size="sm" disabled={offset + PAGE >= total} onClick={() => setOffset(offset + PAGE)}>Next</Btn>
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(200px, 1fr))", gap: 14 }}>
              {photos.map(p => (
                <Card key={p.id} style={{ padding: 0, overflow: "hidden" }}>
                  {p.url_small
                    // eslint-disable-next-line @next/next/no-img-element -- public redirect URL, sized thumbnails
                    ? <a href={p.url_large ?? undefined} target="_blank" rel="noreferrer"><img src={p.url_small} alt={p.place_label ?? p.file_name} loading="lazy" style={{ width: "100%", height: 135, objectFit: "cover", display: "block", background: A.line2 }} /></a>
                    : <div style={{ height: 135, display: "grid", placeItems: "center", background: A.line2, color: A.muted, fontSize: 12 }}>not stored</div>}
                  <div style={{ padding: "10px 12px", fontSize: 12.5 }}>
                    <div style={{ display: "flex", gap: 6, alignItems: "center", marginBottom: 4 }}>
                      <Badge color={STATUS_COLOR[p.status] ?? "gray"}>{p.status}</Badge>
                      {p.match_source === "manual" && <Badge color="blue">manual</Badge>}
                      {p.width && <span style={{ color: A.muted, fontFamily: mono, fontSize: 11 }}>{p.width}×{p.height}</span>}
                    </div>
                    <div style={{ fontWeight: 600, color: A.ink }} title={p.file_name}>{p.place_label || p.file_name}</div>
                    <div style={{ color: A.muted }} title={p.folder_path}>{p.country} · {p.tour_folder ?? "(country root)"}</div>
                    <div style={{ color: A.body, marginTop: 4 }}>Tour: {p.tour_name ?? "—"}</div>
                    {p.destination_name && <div style={{ color: A.body }}>Place: {p.destination_name}</div>}
                    {p.error && <div style={{ color: A.red, fontFamily: mono, fontSize: 11, marginTop: 4 }}>{p.error}</div>}
                    {p.status !== "rejected" && (
                      <div style={{ marginTop: 8 }}>
                        <Btn size="sm" variant="ghost" disabled={busy === p.id} onClick={() => reject(p)}>Reject</Btn>
                      </div>
                    )}
                  </div>
                </Card>
              ))}
            </div>
            {photos.length === 0 && <div style={{ color: A.muted, fontSize: 13 }}>No photos for this filter.</div>}
          </>
        )}
      </main>
  );
}
