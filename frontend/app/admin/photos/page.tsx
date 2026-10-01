"use client";
// app/admin/photos/page.tsx — AA-708 marketing photos synced from the Jira CON board's Google Drive
// folders: coverage per country, the photo grid, the unmatched queue with manual assignment, and a
// "Sync now" button (enqueues a `photo_sync` job, visible on /admin/jobs). API: /admin/photos/*.
// Destination matching stays off until the rerun tours are re-extracted (S207): today
// shared.destinations still holds places from before the reset.

import { useCallback, useEffect, useState } from "react";
import { Image as ImageIcon, RefreshCw } from "lucide-react";
import AdminSidebar from "../_components/AdminSidebar";
import { A, serif, sans, mono, Card, SLabel, Badge, Btn, LoadingScreen, TH, TD } from "../_components/adminUi";

type CountryRow = {
  country: string; photos: number; with_tour: number; with_destination: number; unmatched: number;
  rejected: number; errors: number; tour_folders: number; tours_covered: number; active_tours: number;
  destinations: number; destinations_with_cover: number; last_synced: string | null;
};
type LastJob = { id: string; status: string; error: string | null; created_at: string | null; finished_at: string | null; result: unknown };
type Photo = {
  id: string; country: string; folder_path: string; tour_folder: string | null; file_name: string;
  place_label: string | null; tour_id: string | null; tour_name: string | null; destination_id: string | null;
  destination_name: string | null; match_source: string; status: string; width: number | null;
  height: number | null; error: string | null; url_small: string | null; url_large: string | null;
};
type Option = { id: string; name: string };
type Folder = { folder_path: string; photos: number; unmatched: number; tour_name: string | null; tours: number };

const STATUS_COLOR: Record<string, "green" | "amber" | "gray" | "red"> = {
  matched: "green", unmatched: "amber", rejected: "gray", error: "red",
};
const PAGE = 120;

function fmtTime(s: string | null) {
  return s ? new Date(s).toLocaleString("en-GB", { dateStyle: "short", timeStyle: "short" }) : "—";
}

export default function PhotosPage() {
  const [countries, setCountries] = useState<CountryRow[] | null>(null);
  const [lastJob, setLastJob] = useState<LastJob | null>(null);
  const [photos, setPhotos] = useState<Photo[]>([]);
  const [total, setTotal] = useState(0);
  const [country, setCountry] = useState("");
  const [status, setStatus] = useState("unmatched");
  const [folders, setFolders] = useState<Folder[]>([]);
  const [folder, setFolder] = useState("");
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [assigning, setAssigning] = useState<Photo | null>(null);
  const [tourOpts, setTourOpts] = useState<Option[]>([]);
  const [pickTour, setPickTour] = useState("");
  const [matchPlaces, setMatchPlaces] = useState(false);

  const loadSummary = useCallback(async () => {
    const r = await fetch("/api/admin/photos/summary");
    if (!r.ok) { setError(`Summary failed (${r.status})`); return; }
    const body = await r.json();
    setCountries(body.countries);
    setLastJob(body.last_job);
  }, []);

  const loadPhotos = useCallback(async () => {
    const qs = new URLSearchParams({ limit: String(PAGE), offset: String(offset) });
    if (country) qs.set("country", country);
    if (status) qs.set("status", status);
    if (folder) qs.set("folder_path", folder);
    const r = await fetch(`/api/admin/photos?${qs}`);
    if (!r.ok) { setError(`Photos failed (${r.status})`); return; }
    const body = await r.json();
    setPhotos(body.photos);
    setTotal(body.total);
  }, [country, status, folder, offset]);

  const loadFolders = useCallback(async () => {
    if (!country) { setFolders([]); return; }
    const r = await fetch(`/api/admin/photos/folders?country=${encodeURIComponent(country)}`);
    setFolders(r.ok ? (await r.json()).folders : []);
  }, [country]);

  useEffect(() => { loadSummary(); }, [loadSummary]);
  useEffect(() => { loadPhotos(); }, [loadPhotos]);
  useEffect(() => { loadFolders(); }, [loadFolders]);

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

  async function openAssign(p: Photo) {
    setAssigning(p);
    setPickTour(p.tour_id ?? "");
    const r = await fetch(`/api/admin/photos/tours?country=${encodeURIComponent(p.country)}`);
    setTourOpts(r.ok ? (await r.json()).tours : []);
  }

  async function assign(p: Photo, reject: boolean) {
    setBusy(p.id);
    try {
      const r = await fetch(`/api/admin/photos/${p.id}/assign`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(reject ? { reject: true } : { tour_id: pickTour || null, whole_folder: true }),
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) { setError(`Assign failed: ${body.detail ?? r.status}`); return; }
      setAssigning(null);
      await Promise.all([loadPhotos(), loadSummary(), loadFolders()]);
    } finally { setBusy(null); }
  }

  if (!countries) return <LoadingScreen msg="Loading photos..." />;
  const jobRunning = lastJob && ["queued", "running"].includes(lastJob.status);

  return (
    <div style={{ display: "flex", height: "100vh", background: A.bg, fontFamily: sans }}>
      <AdminSidebar />
      <main style={{ flex: 1, padding: "32px 36px", minWidth: 0, minHeight: 0, overflowY: "auto" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 20, flexWrap: "wrap" }}>
          <div style={{ width: 36, height: 36, borderRadius: 9, background: `${A.accent}15`, color: A.accent, display: "grid", placeItems: "center" }}>
            <ImageIcon size={18} />
          </div>
          <div style={{ flex: 1 }}>
            <h1 style={{ fontFamily: serif, fontSize: 24, margin: 0, color: A.ink }}>Photos</h1>
            <div style={{ fontSize: 12.5, color: A.muted }}>
              Marketing photos from the Contents board (Google Drive) → S3, matched to tours by folder name.
            </div>
          </div>
          <Btn onClick={() => { loadSummary(); loadPhotos(); }} size="sm"><RefreshCw size={13} /> Refresh</Btn>
          <label style={{ fontSize: 12.5, color: A.body, display: "flex", gap: 6, alignItems: "center" }}
                 title="Only after this country's tours are rewritten, atomized and re-extracted by the TripPlanner">
            <input type="checkbox" checked={matchPlaces} onChange={e => setMatchPlaces(e.target.checked)} />
            Match places + covers
          </label>
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
          <div style={{ fontSize: 12, color: A.muted, marginTop: 8 }}>
            Places (destinations) are matched only with "Match places + covers", per country, after its rerun tours are
            re-extracted; a photo is compared only with the places on its own tour&apos;s itinerary.
            Assigning a tour applies to every photo in the same Drive folder.
          </div>
        </Card>

        <Card style={{ marginBottom: 18, padding: 0, overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr>
                {["Country", "Photos", "Tour folders", "Tours covered", "Matched to tour", "Unmatched", "Rejected", "Errors", "Last synced"].map(h => <th key={h} style={TH}>{h}</th>)}
              </tr>
            </thead>
            <tbody>
              {countries.length === 0 && <tr><td style={TD} colSpan={9}>No photos synced yet.</td></tr>}
              {countries.map(c => (
                <tr key={c.country} onClick={() => { setCountry(c.country); setFolder(""); setOffset(0); }} style={{ cursor: "pointer", background: c.country === country ? A.accentTint : undefined }}>
                  <td style={{ ...TD, fontWeight: 600 }}>{c.country}</td>
                  <td style={{ ...TD, fontFamily: mono }}>{c.photos}</td>
                  <td style={{ ...TD, fontFamily: mono }}>{c.tour_folders}</td>
                  <td style={{ ...TD, fontFamily: mono }}>{c.tours_covered} / {c.active_tours}</td>
                  <td style={{ ...TD, fontFamily: mono }}>{c.with_tour}</td>
                  <td style={{ ...TD, fontFamily: mono, color: c.unmatched ? A.amber : undefined }}>{c.unmatched}</td>
                  <td style={{ ...TD, fontFamily: mono }}>{c.rejected}</td>
                  <td style={{ ...TD, fontFamily: mono, color: c.errors ? A.red : undefined }}>{c.errors}</td>
                  <td style={TD}>{fmtTime(c.last_synced)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>

        <div style={{ display: "flex", gap: 10, alignItems: "center", marginBottom: 12, flexWrap: "wrap" }}>
          <select value={country} onChange={e => { setCountry(e.target.value); setFolder(""); setOffset(0); }} style={{ padding: "6px 8px", borderRadius: 8, border: `1px solid ${A.line}` }}>
            <option value="">All countries</option>
            {countries.map(c => <option key={c.country} value={c.country}>{c.country}</option>)}
          </select>
          <select value={status} onChange={e => { setStatus(e.target.value); setOffset(0); }} style={{ padding: "6px 8px", borderRadius: 8, border: `1px solid ${A.line}` }}>
            <option value="">All statuses</option>
            {Object.keys(STATUS_COLOR).map(s => <option key={s} value={s}>{s}</option>)}
          </select>
          {country && (
            <select value={folder} onChange={e => { setFolder(e.target.value); setOffset(0); }} style={{ padding: "6px 8px", borderRadius: 8, border: `1px solid ${A.line}`, maxWidth: 420 }}>
              <option value="">All folders ({folders.length})</option>
              {folders.map(f => (
                <option key={f.folder_path} value={f.folder_path}>
                  {f.folder_path} — {f.photos} photos{f.unmatched ? `, ${f.unmatched} unmatched` : ""}{f.tour_name ? ` → ${f.tour_name}` : ""}
                </option>
              ))}
            </select>
          )}
          <span style={{ fontSize: 12.5, color: A.muted }}>{total} photos</span>
          <div style={{ flex: 1 }} />
          <Btn size="sm" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>Prev</Btn>
          <Btn size="sm" disabled={offset + PAGE >= total} onClick={() => setOffset(offset + PAGE)}>Next</Btn>
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(210px, 1fr))", gap: 14 }}>
          {photos.map(p => (
            <Card key={p.id} style={{ padding: 0, overflow: "hidden" }}>
              {p.url_small
                // eslint-disable-next-line @next/next/no-img-element -- external redirect URL, sized thumbnails
                ? <a href={p.url_large ?? undefined} target="_blank" rel="noreferrer"><img src={p.url_small} alt={p.place_label ?? p.file_name} loading="lazy" style={{ width: "100%", height: 140, objectFit: "cover", display: "block", background: A.line2 }} /></a>
                : <div style={{ height: 140, display: "grid", placeItems: "center", background: A.line2, color: A.muted, fontSize: 12 }}>no file</div>}
              <div style={{ padding: "10px 12px", fontSize: 12.5 }}>
                <div style={{ display: "flex", gap: 6, alignItems: "center", marginBottom: 4 }}>
                  <Badge color={STATUS_COLOR[p.status] ?? "gray"}>{p.status}</Badge>
                  {p.match_source === "manual" && <Badge color="blue">manual</Badge>}
                </div>
                <div style={{ fontWeight: 600, color: A.ink }} title={p.file_name}>{p.place_label || p.file_name}</div>
                <div style={{ color: A.muted }} title={p.folder_path}>{p.country} · {p.tour_folder ?? "(country root)"}</div>
                <div style={{ color: A.body, marginTop: 4 }}>Tour: {p.tour_name ?? "—"}</div>
                {p.error && <div style={{ color: A.red, fontFamily: mono, fontSize: 11, marginTop: 4 }}>{p.error}</div>}
                {assigning?.id === p.id ? (
                  <div style={{ marginTop: 8, display: "grid", gap: 6 }}>
                    <select value={pickTour} onChange={e => setPickTour(e.target.value)} style={{ padding: 5, borderRadius: 6, border: `1px solid ${A.line}` }}>
                      <option value="">Choose a tour…</option>
                      {tourOpts.map(t => <option key={t.id} value={t.id}>{t.name}</option>)}
                    </select>
                    <div style={{ display: "flex", gap: 6 }}>
                      <Btn size="sm" variant="primary" disabled={!pickTour || busy === p.id} onClick={() => assign(p, false)}>Save</Btn>
                      <Btn size="sm" onClick={() => setAssigning(null)}>Cancel</Btn>
                    </div>
                  </div>
                ) : (
                  <div style={{ marginTop: 8, display: "flex", gap: 6 }}>
                    <Btn size="sm" onClick={() => openAssign(p)}>Assign folder to tour</Btn>
                    {p.status !== "rejected" && <Btn size="sm" variant="ghost" disabled={busy === p.id} onClick={() => assign(p, true)}>Reject</Btn>}
                  </div>
                )}
              </div>
            </Card>
          ))}
        </div>
        {photos.length === 0 && <div style={{ color: A.muted, fontSize: 13 }}>No photos for this filter.</div>}
      </main>
    </div>
  );
}
