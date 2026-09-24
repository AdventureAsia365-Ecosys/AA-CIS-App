"use client";
// app/(tenant)/portal/_components/CatalogTab.tsx
// API: GET   /api/tenant/v1/tours/my-versions?page_size=50
//      GET   /api/tenant/v1/tours/versions/{id}
//      PATCH /api/tenant/v1/tours/versions/{id}          (action: "edit" — Save)
//      POST  /api/tenant/v1/tours/pool/{published_tour_id}/rewrite   (Request Rewrite)
//
// AA-565 — rebuilt as a real table (Admin Master Content style) + right-side overlay drawer.
// The version/approval-status concept (Queued/In Catalog/Ready to Review/New Version Requested,
// "AI Generated" labels, manual "Add to Catalog") is intentionally not exposed to tenants
// anymore — see the AA-565 Linear thread for the full decision trail. The underlying DB keeps
// every version row exactly as before (Admin's oversight views still read the full history) —
// this file only ever shows the newest version per tour and never calls the approve/reject
// PATCH actions.

import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import * as XLSX from "xlsx";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Save, X, Download, Package, PenLine } from "lucide-react";
import {
  T, mono, sans,
  Btn, LoadingScreen, EmptyState,
  parseHighlights, parseContent, fmtDateTime,
} from "./ui";
import { SeeOriginalToggle } from "./SeeOriginalToggle";

interface Version {
  id: string; version_number: number; status: string;
  quality_score: number | null; edit_source: string;
  rewrite_language: string; created_at: string; edited_at: string | null;
  rewritten_content: string; seo_mode: string; aa_name: string;
  aa_subtitle: string; aa_summary: string; aa_highlights: string;
  aa_itineraries: string | null; aa_seo_title: string; aa_seo_meta: string;
  aa_quality_score: number; country: string | null; duration: string | null;
  inclusions?: string | null; exclusions?: string | null; // AA-566 Phần B.4
  published_tour_id?: string;
  tour_id?: string;  // AA-454 — raw_tours.tour_id, used for the ?tour_id= "showing versions for
                      // this tour" filter below (AA-526 — no longer also a deep-link target into
                      // AtomsTab/T6, removed along with tenant atom visibility)
}

// AA-566 Phần B.3 — sortable columns, same toggleSort/arrow convention as
// admin/_components/auditPanels.tsx (AA-557's own precedent).
type SortKey = "aa_name" | "country";

// AA-565 — a version is "still being written by AI" only while status='pending' AND
// edit_source='ai_generated'. A tenant's own manual Save also inserts a status='pending' row
// (edit_source='tenant_edit') that never transitions to anything else — treating ANY pending
// row as "writing" would (a) show a false loading state right after a manual edit, and (b) keep
// the 5s poll below running forever for any tenant who has ever clicked Save once. Both are real
// bugs, not hypothetical — confirmed by reading update_version()'s edit branch in
// api/routers/v1_tours.py, which never advances a tenant_edit row past 'pending'.
function isAiWriting(v: Pick<Version, "status" | "edit_source">): boolean {
  return v.status === "pending" && v.edit_source === "ai_generated";
}

export default function CatalogTab() {
  // AA-454 — a ?tour_id= filters the list to just that original tour's versions. Client-side
  // only (list is already fetched page_size=50, no backend param needed).
  const searchParams = useSearchParams();
  const tourIdFilter = searchParams.get("tour_id");

  const [list, setList]         = useState<Version[]>([]);
  const [loading, setLoading]   = useState(true);
  const [search, setSearch]     = useState("");
  const [countryFilter, setCountryFilter] = useState("");
  const [sortKey, setSortKey]   = useState<SortKey | null>(null);
  const [sortDir, setSortDir]   = useState<"asc" | "desc">("asc");
  const [exportingDocxId, setExportingDocxId] = useState<string | null>(null);

  // AA-565 — the open drawer is identified by published_tour_id, not a specific version id.
  // Every action here (Save, Request Rewrite) creates a NEW version row under the hood; keying
  // by published_tour_id means the drawer automatically tracks whichever version is now newest
  // for that tour once `list` refetches, with no separate "stale id" bookkeeping.
  const [openTourId, setOpenTourId] = useState<string | null>(null);
  const [detail, setDetail]     = useState<Version | null>(null);
  const [dlLoad, setDlLoad]     = useState(false);
  const [saving, setSaving]     = useState(false);
  const [saveOk, setSaveOk]     = useState(false);
  const [dirty, setDirty]       = useState(false);
  const [expandItin, setExpandItin] = useState(false);
  const [localToast, setLocalToast] = useState<string | null>(null);
  const [showRewriteConfirm, setShowRewriteConfirm] = useState(false);
  const [rewriting, setRewriting] = useState(false);
  // Original AA tour data for the "AA Original" diff column
  const [origTour, setOrigTour]     = useState<any>(null);

  // AA-28 multi-select export
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [isExporting, setIsExporting] = useState(false);

  // Refs for polling — pollingRef holds the interval ID so we never double-start
  const pollingRef  = useRef<ReturnType<typeof setInterval> | null>(null);
  const listRef     = useRef<Version[]>([]);

  // Edit state
  const [editName, setEditName]       = useState("");
  const [editSubtitle, setEditSubtitle] = useState("");
  const [editSummary, setEditSummary] = useState("");
  const [editHighlights, setEditHighlights] = useState<string[]>([]);
  const [editSeoTitle, setEditSeoTitle] = useState("");
  const [editSeoMeta, setEditSeoMeta] = useState("");

  const fetchList = useCallback(async () => {
    setLoading(true);
    try {
      const r = await fetch(`/api/tenant/v1/tours/my-versions?page_size=50`);
      if (r.ok) { const d = await r.json(); setList(d.data ?? []); }
    } finally { setLoading(false); }
  }, []);

  useEffect(() => { fetchList(); }, [fetchList]);

  // AA-565 — 1 row per tour: group the flat my-versions response by published_tour_id, keeping
  // only the highest version_number per group. Pure client-side reduction, no backend change —
  // old versions stay in the DB and stay visible to Admin, just not surfaced here.
  const grouped = useMemo(() => {
    const byTour = new Map<string, Version>();
    for (const v of list) {
      if (!v.published_tour_id) continue;
      const cur = byTour.get(v.published_tour_id);
      if (!cur || v.version_number > cur.version_number) byTour.set(v.published_tour_id, v);
    }
    return [...byTour.values()].sort((a, b) => (a.created_at < b.created_at ? 1 : -1));
  }, [list]);

  const openGroup = useMemo(
    () => (openTourId ? grouped.find(v => v.published_tour_id === openTourId) ?? null : null),
    [grouped, openTourId]
  );

  useEffect(() => { listRef.current = list; }, [list]);

  // Polling: start when an AI-generated row is still pending, stop on completion or unmount.
  // Scoped to isAiWriting (not raw status==='pending') — see isAiWriting()'s own comment for why.
  useEffect(() => {
    const hasWriting = list.some(isAiWriting);

    if (!hasWriting) {
      if (pollingRef.current) { clearInterval(pollingRef.current); pollingRef.current = null; }
      return;
    }
    if (pollingRef.current) return; // already running

    const startTime = Date.now();
    pollingRef.current = setInterval(async () => {
      if (Date.now() - startTime > 300_000) {
        if (pollingRef.current) { clearInterval(pollingRef.current); pollingRef.current = null; }
        return;
      }
      try {
        const r = await fetch('/api/tenant/v1/tours/my-versions?page_size=50');
        if (!r.ok) return;
        const fresh: Version[] = (await r.json()).data ?? [];

        const stillWriting = fresh.some(isAiWriting);
        if (!stillWriting) {
          if (pollingRef.current) { clearInterval(pollingRef.current); pollingRef.current = null; }
          const justDone = fresh.filter(v =>
            !isAiWriting(v) && listRef.current.find(o => o.id === v.id && isAiWriting(o))
          );
          setList(fresh);
          if (justDone.length > 0) {
            const name = justDone[0].aa_name || 'Tour';
            setLocalToast(`"${name}" is ready. Click to review.`);
            setTimeout(() => setLocalToast(null), 5000);
          }
          return;
        }
        setList(fresh);
      } catch {}
    }, 5000);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [list]);

  // Cleanup polling on unmount
  useEffect(() => () => {
    if (pollingRef.current) { clearInterval(pollingRef.current); pollingRef.current = null; }
  }, []);

  const loadDetail = useCallback(async (v: Version) => {
    setDlLoad(true); setDetail(null); setOrigTour(null);
    setDirty(false); setSaveOk(false); setExpandItin(true);
    try {
      const [detailResult, origResult] = await Promise.allSettled([
        fetch(`/api/tenant/v1/tours/versions/${v.id}`),
        v.published_tour_id
          ? fetch(`/api/tenant/v1/tours/pool/${v.published_tour_id}`)
          : Promise.reject(new Error('no published_tour_id')),
      ]);

      if (detailResult.status === 'rejected' || !detailResult.value.ok) return;
      const d: Version = await detailResult.value.json();
      setDetail(d);

      let orig: Record<string, unknown> | null = null;
      if (origResult.status === 'fulfilled' && origResult.value.ok) {
        orig = await origResult.value.json();
      }
      if (!orig) {
        orig = {
          aa_summary:     d.aa_summary     ?? null,
          seo_title:      d.aa_seo_title   ?? null,
          seo_meta:       d.aa_seo_meta    ?? null,
          aa_highlights:  d.aa_highlights  ?? null,
          aa_itineraries: d.aa_itineraries ?? null,
        };
      }
      setOrigTour(orig);

      const rc = parseContent(d.rewritten_content) as Record<string, unknown> | null;
      setEditName((rc?.name ?? d.aa_name ?? "") as string);
      setEditSubtitle((rc?.subtitle ?? d.aa_subtitle ?? "") as string);
      setEditSummary((rc?.summary ?? d.aa_summary ?? "") as string);
      setEditHighlights(Array.isArray(rc?.highlights) ? rc.highlights as string[] : parseHighlights(d.aa_highlights));
      setEditSeoTitle((rc?.seo_title ?? d.aa_seo_title ?? "") as string);
      setEditSeoMeta((rc?.seo_meta ?? d.aa_seo_meta ?? "") as string);
    } finally { setDlLoad(false); }
  }, []);

  // Whenever the open tour's latest version changes (drawer opened, Save/Request Rewrite
  // produced a new version, or polling picked up completion) — load its content, unless it's
  // still being AI-written (loading placeholder handles that case instead).
  useEffect(() => {
    if (!openGroup) { setDetail(null); return; }
    if (isAiWriting(openGroup)) { setDetail(null); return; }
    if (detail?.id === openGroup.id) return;
    loadDetail(openGroup);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openGroup?.id, openGroup?.status]);

  function closeDrawer() {
    setOpenTourId(null); setDetail(null); setDirty(false); setSaveOk(false);
  }

  async function saveEdit() {
    if (!openGroup || !dirty) return;
    setSaving(true); setSaveOk(false);
    try {
      const r = await fetch(`/api/tenant/v1/tours/versions/${openGroup.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          action: "edit",
          edited_content: { name: editName, subtitle: editSubtitle, summary: editSummary, highlights: editHighlights, seo_title: editSeoTitle, seo_meta: editSeoMeta },
          edited_by: "tenant",
        }),
      });
      if (r.ok) {
        const body = await r.json(); // { status, new_version_id, version_number }
        setSaveOk(true); setDirty(false);
        // Point detail at the new version id BEFORE refetching the list: the openGroup-sync
        // effect below reloads detail whenever openGroup.id !== detail.id, which would
        // otherwise immediately flip saveOk back to false (loadDetail() resets it) the instant
        // fetchList() picks up the new version this same edit just created — found live during
        // this build's own Playwright verify (the "✓ Saved" confirmation was flashing then
        // disappearing within the same render pass).
        if (body.new_version_id) setDetail(prev => prev ? { ...prev, id: body.new_version_id } : prev);
        await fetchList();
      }
    } finally { setSaving(false); }
  }

  // AA-565 — real rewrite, not the old "just mark rejected" no-op. Same endpoint + same quota
  // check Browse Pool's first-time write uses (api/routers/v1_tours.py POST
  // /pool/{id}/rewrite) — this consumes a real rewrite credit, confirmed accepted by Nghiệp.
  async function requestRewrite() {
    if (!openGroup?.published_tour_id) return;
    setShowRewriteConfirm(false);
    setRewriting(true);
    try {
      const r = await fetch(`/api/tenant/v1/tours/pool/${openGroup.published_tour_id}/rewrite`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rewrite_language: openGroup.rewrite_language || "en-US" }),
      });
      if (r.ok) { setDetail(null); await fetchList(); }
    } finally { setRewriting(false); }
  }

  function flattenVal(v: any): string {
    if (v == null || v === "") return "";
    if (typeof v === "string") {
      try { return flattenVal(JSON.parse(v)); } catch {}
      return v.replace(/[\t\n\r]+/g, " ").trim();
    }
    if (Array.isArray(v)) {
      return v.map((x: any) => {
        if (x == null) return "";
        if (typeof x === "string") return x.replace(/[\t\n\r]+/g, " ").trim();
        if (typeof x === "object") return String(x.keyword ?? x.text ?? x.description ?? x.title ?? x.value ?? JSON.stringify(x));
        return String(x);
      }).filter(Boolean).join(" | ");
    }
    if (typeof v === "object") {
      return Object.values(v).filter(x => x != null && x !== "").map(x => String(x)).join(" | ");
    }
    return String(v);
  }

  async function exportSelected(fmt: "csv" | "xls") {
    const selectedVersions = grouped.filter(v => selectedIds.has(v.id));
    if (selectedVersions.length === 0) return;
    setIsExporting(true);
    try {
      const fullData = await Promise.all(
        selectedVersions.map(async v => {
          try {
            const r = await fetch(`/api/tenant/v1/tours/versions/${v.id}`);
            return r.ok ? await r.json() : null;
          } catch { return null; }
        })
      );

      // AA-565 — Quality Score/Version/Status dropped from the export too, same reasoning as
      // the UI: internal/technical fields the tenant doesn't need. Language kept (their own
      // chosen output language, not an approval-status word).
      const headers = [
        "Name", "Subtitle", "Country", "Duration",
        "SEO Title", "SEO Meta", "Summary", "Highlights", "Itineraries",
        "Created At", "Language",
      ];

      const rows: Record<string, string>[] = fullData.map((d, i) => {
        const v  = selectedVersions[i];
        const rc = parseContent(d?.rewritten_content ?? v.rewritten_content) as Record<string, unknown> | null;
        return {
          "Name":          String(rc?.name        ?? d?.aa_name      ?? v.aa_name      ?? ""),
          "Subtitle":      String(rc?.subtitle    ?? d?.aa_subtitle  ?? ""),
          "Country":       String(d?.country      ?? v.country       ?? ""),
          "Duration":      String(d?.duration     ?? v.duration      ?? ""),
          "SEO Title":     String(rc?.seo_title   ?? d?.aa_seo_title ?? ""),
          "SEO Meta":      String(rc?.seo_meta    ?? d?.aa_seo_meta  ?? ""),
          "Summary":       String(rc?.summary     ?? d?.aa_summary   ?? ""),
          "Highlights":    flattenVal(rc?.highlights    ?? d?.aa_highlights    ?? ""),
          "Itineraries":   flattenVal(rc?.itineraries   ?? d?.aa_itineraries   ?? ""),
          "Created At":    v.created_at ? new Date(v.created_at).toLocaleDateString("en-GB") : "",
          "Language":      v.rewrite_language,
        };
      });

      if (fmt === "csv") {
        const san = (s: string) => s.replace(/[\t\n\r]+/g, " ");
        const tsv = [headers.join("\t"), ...rows.map(r => headers.map(h => san(r[h] ?? "")).join("\t"))].join("\n");
        const blob = new Blob(["﻿" + tsv], { type: "text/csv;charset=utf-8" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a"); a.href = url; a.download = "my-catalog.csv"; a.click(); URL.revokeObjectURL(url);
      } else {
        const ws = XLSX.utils.json_to_sheet(rows, { header: headers });
        ws["!cols"] = [30,30,15,12,40,50,60,60,80,15,15].map(w => ({ wch: w }));
        const wb = XLSX.utils.book_new();
        XLSX.utils.book_append_sheet(wb, ws, "My Catalog Tours");
        XLSX.writeFile(wb, "my-catalog.xlsx");
      }
    } finally { setIsExporting(false); }
  }

  // AA-566 Phần B.3 — Country dropdown filter (Score/Version filters were already dropped in
  // AA-565 — the tenant view no longer surfaces those fields at all).
  const countries = useMemo(
    () => [...new Set(grouped.map(g => g.country).filter(Boolean))].sort() as string[],
    [grouped]
  );

  function toggleSort(key: SortKey) {
    if (sortKey !== key) { setSortKey(key); setSortDir("asc"); }
    else if (sortDir === "asc") setSortDir("desc");
    else { setSortKey(null); setSortDir("asc"); }
  }
  const sortArrow = (key: SortKey) => (sortKey === key ? (sortDir === "asc" ? "▲" : "▼") : "▲▼");

  const visibleList = useMemo(() => {
    let v = tourIdFilter ? grouped.filter(g => g.tour_id === tourIdFilter) : grouped;
    if (countryFilter) v = v.filter(g => g.country === countryFilter);
    if (search.trim()) {
      const q = search.trim().toLowerCase();
      v = v.filter(g => (g.aa_name || "").toLowerCase().includes(q) || (g.country || "").toLowerCase().includes(q));
    }
    if (sortKey) {
      v = [...v].sort((a, b) => {
        const av = (a[sortKey] || "").toString().toLowerCase();
        const bv = (b[sortKey] || "").toString().toLowerCase();
        const cmp = av < bv ? -1 : av > bv ? 1 : 0;
        return sortDir === "asc" ? cmp : -cmp;
      });
    }
    return v;
  }, [grouped, tourIdFilter, search, countryFilter, sortKey, sortDir]);

  const allSelected = visibleList.length > 0 && visibleList.every(v => selectedIds.has(v.id));

  // AA-566 Phần B.2 — real DOCX export, mirrors Admin Master Content's own
  // exportVersionDocx()/downloadBlob() trigger pattern (frontend/app/admin/master-content/
  // page.tsx), pointed at the new tenant-scoped endpoint (GET /v1/tours/versions/{id}/
  // export-docx) instead of Admin's silver_aa_internal.generated_content-reading one.
  async function exportDocx(versionId: string) {
    setExportingDocxId(versionId);
    try {
      const r = await fetch(`/api/tenant/v1/tours/versions/${versionId}/export-docx`);
      if (!r.ok) return;
      const blob = await r.blob();
      const cd = r.headers.get("content-disposition") || "";
      const match = /filename=([^;]+)/.exec(cd);
      const filename = match ? match[1].trim() : "tour.docx";
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = filename; a.click();
      URL.revokeObjectURL(url);
    } finally { setExportingDocxId(null); }
  }

  return (
    <>
    <style>{`@keyframes cis-spin { to { transform: rotate(360deg); } } @keyframes cis-pulse { 0%, 100% { opacity: .4; } 50% { opacity: 1; } }`}</style>

    {/* Local toast — bottom-right, auto-dismiss after 5s */}
    {localToast && (
      <div style={{
        position: "fixed", bottom: 24, right: 28, zIndex: 9999,
        padding: "12px 20px", background: "#16A34A", borderRadius: 10,
        color: "#fff", fontSize: 13, fontWeight: 600,
        boxShadow: "0 4px 20px rgba(0,0,0,0.2)", maxWidth: 380,
      }}>
        {localToast}
      </div>
    )}

    {/* AA-454 — arrived via a ?tour_id= deep link */}
    {tourIdFilter && (
      <div style={{
        display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10,
        padding: "9px 14px", marginBottom: 12, borderRadius: 8,
        background: T.goldTint, border: `1px solid ${T.goldSoft}`, fontFamily: sans,
      }}>
        <span style={{ fontSize: 12, color: T.amber }}>
          Showing versions for: <strong>{visibleList[0]?.aa_name ?? "this tour"}</strong>
        </span>
        <Link href="/portal/t4-pool" style={{ fontSize: 12, color: T.amber, fontWeight: 600, textDecoration: "none" }}>
          Clear filter ×
        </Link>
      </div>
    )}

    {/* Sticky filter row */}
    <div style={{
      position: "sticky", top: 0, zIndex: 5, background: T.bg,
      display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12,
      flexWrap: "wrap", padding: "10px 0", marginBottom: 4, borderBottom: `1px solid ${T.line}`,
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <span style={{ fontSize: 13, fontWeight: 700, color: T.ink, fontFamily: sans }}>My Catalog Tours</span>
        <span style={{ fontSize: 12, color: T.muted2 }}>
          {visibleList.length < grouped.length ? `${visibleList.length} of ${grouped.length}` : `${grouped.length}`} tours
        </span>
      </div>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <input
          value={search}
          onChange={e => setSearch(e.target.value)}
          placeholder="Search name or country…"
          style={{ padding: "5px 10px", border: `1px solid ${T.line}`, borderRadius: 6, fontSize: 12, fontFamily: sans, width: 200, background: T.card, color: T.ink, outline: "none" }}
        />
        {countries.length > 0 && (
          <select
            value={countryFilter}
            onChange={e => setCountryFilter(e.target.value)}
            style={{ padding: "5px 10px", border: `1px solid ${T.line}`, borderRadius: 6, fontSize: 12, fontFamily: sans, background: T.card, color: T.ink, outline: "none", cursor: "pointer" }}
          >
            <option value="">All countries</option>
            {countries.map(c => <option key={c} value={c}>{c}</option>)}
          </select>
        )}
        {visibleList.length > 0 && (
          <label style={{ display: "flex", alignItems: "center", gap: 5, fontSize: 11.5, color: T.muted, cursor: "pointer", fontFamily: sans }}>
            <input type="checkbox" checked={allSelected}
              onChange={() => {
                if (allSelected) setSelectedIds(new Set());
                else setSelectedIds(new Set(visibleList.map(v => v.id)));
              }}
              style={{ cursor: "pointer", accentColor: T.gold }} />
            Select all
          </label>
        )}
        {selectedIds.size > 0 && (
          <>
            <button onClick={() => exportSelected("csv")} disabled={isExporting}
              style={{ padding: "5px 12px", borderRadius: 20, fontSize: 11.5, fontWeight: 600, border: "none", background: isExporting ? "#86EFAC" : "#22C55E", color: "#fff", cursor: isExporting ? "default" : "pointer", fontFamily: sans }}>
              {isExporting ? "Fetching…" : `↓ CSV (${selectedIds.size})`}
            </button>
            <button onClick={() => exportSelected("xls")} disabled={isExporting}
              style={{ padding: "5px 12px", borderRadius: 20, fontSize: 11.5, fontWeight: 600, border: "none", background: T.gold, color: "#fff", cursor: isExporting ? "default" : "pointer", fontFamily: sans, opacity: isExporting ? 0.6 : 1 }}>
              {isExporting ? "Fetching…" : `↓ XLSX (${selectedIds.size})`}
            </button>
          </>
        )}
      </div>
    </div>

    {loading ? <LoadingScreen message="Loading catalog…" /> :
     visibleList.length === 0 ? (
       <EmptyState icon={<Package size={32} strokeWidth={1.5} color={T.gold} />} title="No rewrites yet" sub="Browse the pool and rewrite your first tour" />
     ) : (
      <div style={{ overflowX: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead>
            <tr style={{ borderBottom: `2px solid ${T.line}` }}>
              <th style={{ ...THStyle, width: 36 }}>
                <input type="checkbox" checked={allSelected}
                  onChange={() => {
                    if (allSelected) setSelectedIds(new Set());
                    else setSelectedIds(new Set(visibleList.map(v => v.id)));
                  }}
                  style={{ accentColor: T.gold, cursor: "pointer" }} />
              </th>
              <th style={{ ...THStyle, width: 36 }}>#</th>
              <th style={THStyle}>
                <button onClick={() => toggleSort("aa_name")} style={{ ...sortBtnStyle, color: sortKey === "aa_name" ? T.gold : T.muted }}>
                  Tour Name <span style={{ fontSize: 9, opacity: sortKey === "aa_name" ? 1 : 0.35 }}>{sortArrow("aa_name")}</span>
                </button>
              </th>
              <th style={THStyle}>
                <button onClick={() => toggleSort("country")} style={{ ...sortBtnStyle, color: sortKey === "country" ? T.gold : T.muted }}>
                  Country <span style={{ fontSize: 9, opacity: sortKey === "country" ? 1 : 0.35 }}>{sortArrow("country")}</span>
                </button>
              </th>
              <th style={{ ...THStyle, textAlign: "right" as const }}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {visibleList.map((v, i) => {
              const writing = isAiWriting(v);
              return (
                <tr key={v.id}
                  onClick={() => setOpenTourId(v.published_tour_id ?? null)}
                  style={{
                    background: i % 2 === 0 ? T.card : T.bg,
                    borderBottom: `1px solid ${T.line}`,
                    cursor: "pointer",
                  }}>
                  <td style={TDStyle} onClick={e => e.stopPropagation()}>
                    <input type="checkbox" checked={selectedIds.has(v.id)}
                      onChange={() => setSelectedIds(prev => { const n = new Set(prev); selectedIds.has(v.id) ? n.delete(v.id) : n.add(v.id); return n; })}
                      style={{ accentColor: T.gold, cursor: "pointer" }} />
                  </td>
                  <td style={{ ...TDStyle, color: T.muted2 }}>{i + 1}</td>
                  <td style={{ ...TDStyle, fontWeight: 600, color: T.ink }}>
                    {v.aa_name || "Tour"}
                    {v.aa_subtitle && (
                      <div style={{ fontWeight: 400, fontSize: 11.5, color: T.muted, marginTop: 2 }}>{v.aa_subtitle}</div>
                    )}
                  </td>
                  <td style={TDStyle}>{v.country || "—"}</td>
                  <td style={{ ...TDStyle, textAlign: "right" as const }}>
                    {writing ? (
                      <span style={{ fontSize: 12, color: T.amber, fontFamily: sans, display: "inline-flex", alignItems: "center", gap: 6 }}>
                        <PenLine size={13} style={{ animation: "cis-pulse 1.4s ease-in-out infinite" }} /> Writing…
                      </span>
                    ) : (
                      <button onClick={e => { e.stopPropagation(); setOpenTourId(v.published_tour_id ?? null); }}
                        style={{ padding: "5px 12px", fontSize: 12, fontWeight: 600, border: `1px solid ${T.gold}`, borderRadius: 6, background: T.goldTint, color: T.gold, cursor: "pointer", fontFamily: sans }}>
                        Open →
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    )}

    {/* RIGHT — overlay drawer, reuses admin/_components/TourDetailPanelV2's mechanics */}
    {openGroup && (
      <>
        <div
          onClick={() => { if (!dirty) closeDrawer(); }}
          style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.4)", zIndex: 199 }}
        />
        <div style={{
          // AA-584: was a fixed 700px — t1-rewrite's equivalent panel is a 64%-of-container
          // grid column, so it scales with the screen and reads far wider on anything past a
          // laptop viewport. clamp() keeps the 700px floor (unchanged on narrow/mobile, where
          // maxWidth:92vw already governs) but lets it grow toward t1-rewrite's proportions on
          // wide screens, capped at 1100px so it doesn't become an unreadably long line length.
          position: "fixed", top: 0, right: 0, bottom: 0, width: "clamp(700px, 62vw, 1100px)", maxWidth: "92vw",
          background: T.card, boxShadow: "-4px 0 32px rgba(0,0,0,0.14)", zIndex: 200,
          display: "flex", flexDirection: "column", fontFamily: sans,
        }}>
          <div style={{ padding: "14px 22px", borderBottom: `1px solid ${T.line}`, background: T.bg, display: "flex", justifyContent: "space-between", alignItems: "center", flexShrink: 0 }}>
            <div>
              <div style={{ fontSize: 15, fontWeight: 700, color: T.ink }}>{openGroup.aa_name}</div>
              {!isAiWriting(openGroup) && (
                <div style={{ fontSize: 11.5, color: T.muted, marginTop: 3, fontFamily: mono }}>
                  {openGroup.rewrite_language}
                </div>
              )}
            </div>
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              {dirty && (
                <Btn variant="primary" size="sm" disabled={saving} onClick={saveEdit}>
                  <Save size={12} /> {saving ? "Saving…" : "Save"}
                </Btn>
              )}
              {saveOk && !dirty && <span style={{ fontSize: 12, color: T.green, fontWeight: 600 }}>✓ Saved</span>}
              <button onClick={closeDrawer} style={{ background: "none", border: "none", cursor: "pointer", color: T.muted2 }}>
                <X size={16} />
              </button>
            </div>
          </div>

          {isAiWriting(openGroup) ? (
            <div style={{ flex: 1, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: 12, padding: 40 }}>
              <PenLine size={32} strokeWidth={1.5} color={T.gold} style={{ animation: "cis-pulse 1.4s ease-in-out infinite" }} />
              <div style={{ fontSize: 14, fontWeight: 600, color: T.ink, textAlign: "center" as const }}>
                Writing your tour content…
              </div>
              <div style={{ fontSize: 12, color: T.muted, textAlign: "center" as const }}>
                Usually 1–2 minutes. You can close this and keep browsing — it will be ready in My Catalog Tours.
              </div>
            </div>
          ) : dlLoad ? (
            <LoadingScreen message="Loading your content…" />
          ) : detail ? (
            <div style={{ flex: 1, overflowY: "auto", minHeight: 0 }}>
              <div style={{ padding: "18px 22px", borderBottom: `1px solid ${T.line}` }}>
                <div style={{ fontSize: 10.5, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.12em", color: T.muted, marginBottom: 14 }}>
                  Your Version
                </div>
                <div style={{ display: "flex", gap: 8, marginBottom: 12, fontSize: 11, color: T.muted2, fontFamily: mono }}>
                  <span>Created: {fmtDateTime(detail.created_at)}</span>
                  {detail.edited_at && <span>· Edited: {fmtDateTime(detail.edited_at)}</span>}
                </div>
                {[
                  { label: "Summary", orig: origTour?.aa_summary ?? detail.aa_summary, yours: editSummary, set: (v: string) => { setEditSummary(v); setDirty(true); } },
                ].map(row => (
                  <CompareRow key={row.label} label={row.label} original={row.orig} yours={row.yours} onEdit={row.set} />
                ))}
                <HighlightsCompare
                  origRaw={origTour?.aa_highlights ?? detail.aa_highlights}
                  yours={editHighlights}
                  onChange={h => { setEditHighlights(h); setDirty(true); }}
                />
                {((origTour?.aa_itineraries ?? detail.aa_itineraries) || Boolean(parseContent(detail.rewritten_content)?.itineraries)) && (
                  <ItineraryCompare
                    orig={origTour?.aa_itineraries ?? detail.aa_itineraries ?? ""}
                    yours={String(parseContent(detail.rewritten_content)?.itineraries ?? "")}
                    expand={expandItin}
                    setExpand={setExpandItin}
                  />
                )}
                <SeeOriginalToggle
                  summary={String(origTour?.aa_summary ?? detail.aa_summary ?? "")}
                  seoTitle={null}
                  seoMeta={null}
                  highlightsRaw={String(origTour?.aa_highlights ?? detail.aa_highlights ?? "")}
                  itineraries={String(origTour?.aa_itineraries ?? detail.aa_itineraries ?? "") || null}
                />
              </div>

              {/* AA-566 Phần B.4 — trip facts already in the data model but not previously
                  surfaced to the tenant. SEO Health Bar / SEO Title / SEO Meta deliberately
                  removed from this drawer per Phần B.5 (Nghiệp: not meaningful to tenants) —
                  the underlying columns/fields are untouched, still readable by Admin. */}
              {(detail.duration || detail.inclusions || detail.exclusions) && (
                <div style={{ padding: "14px 22px", borderBottom: `1px solid ${T.line}` }}>
                  <div style={{ fontSize: 10.5, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.12em", color: T.muted, marginBottom: 10 }}>
                    Trip Details
                  </div>
                  {detail.duration && <TripFact label="Duration" value={detail.duration} />}
                  {detail.inclusions && <TripFact label="Inclusions" value={detail.inclusions} />}
                  {detail.exclusions && <TripFact label="Exclusions" value={detail.exclusions} />}
                </div>
              )}

              <div style={{ padding: "14px 22px", display: "flex", justifyContent: "flex-end", gap: 8 }}>
                <Btn variant="ghost" disabled={exportingDocxId === detail.id} onClick={() => exportDocx(detail.id)}>
                  <Download size={12} /> {exportingDocxId === detail.id ? "Exporting…" : "Export DOCX"}
                </Btn>
                <Btn variant="secondary" disabled={rewriting} onClick={() => setShowRewriteConfirm(true)}>
                  {rewriting ? "Starting…" : "Request Rewrite"}
                </Btn>
              </div>
            </div>
          ) : null}
        </div>
      </>
    )}

    {/* Rewrite confirm dialog */}
    {showRewriteConfirm && (
      <div style={{
        position: "fixed", inset: 0, background: "rgba(0,0,0,0.2)",
        display: "flex", alignItems: "center", justifyContent: "center", zIndex: 9999,
      }}>
        <div style={{
          background: "#fff", borderRadius: 16, padding: "24px 28px",
          maxWidth: 380, width: "calc(100% - 32px)",
          boxShadow: "0 8px 32px rgba(0,0,0,0.15)", fontFamily: sans,
        }}>
          <h3 style={{ fontWeight: 700, color: T.ink, marginBottom: 8, fontSize: 15, margin: "0 0 8px" }}>
            Rewrite this tour again?
          </h3>
          <p style={{ fontSize: 13, color: T.muted, margin: "0 0 6px" }}>
            We will generate a fresh version using your brand rules. This uses <strong>1 rewrite credit</strong>.
          </p>
          <div style={{ display: "flex", gap: 10, marginTop: 20 }}>
            <button
              onClick={() => setShowRewriteConfirm(false)}
              style={{ flex: 1, padding: "10px 0", borderRadius: 8, border: `1px solid ${T.line}`, background: T.card, fontSize: 13, color: T.muted, cursor: "pointer", fontFamily: sans }}
            >
              Cancel
            </button>
            <button
              onClick={requestRewrite}
              style={{ flex: 1, padding: "10px 0", borderRadius: 8, border: "none", background: T.gold, fontSize: 13, fontWeight: 600, color: T.ink, cursor: "pointer", fontFamily: sans }}
            >
              Confirm & Rewrite
            </button>
          </div>
        </div>
      </div>
    )}
    </>
  );
}

const THStyle: React.CSSProperties = {
  textAlign: "left", padding: "8px 12px", fontSize: 10.5, fontWeight: 700,
  textTransform: "uppercase", letterSpacing: "0.08em", color: T.muted, fontFamily: sans,
};
const TDStyle: React.CSSProperties = {
  padding: "10px 12px", fontSize: 13, color: T.body, fontFamily: sans,
};
const sortBtnStyle: React.CSSProperties = {
  background: "none", border: "none", cursor: "pointer", padding: 0,
  display: "flex", alignItems: "center", gap: 4, font: "inherit", textTransform: "inherit", letterSpacing: "inherit",
};

function TripFact({ label, value }: { label: string; value: string }) {
  return (
    <div style={{ marginBottom: 10 }}>
      <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: T.muted, marginBottom: 4 }}>{label}</div>
      <div style={{ fontSize: 11.5, color: T.body, lineHeight: 1.6, whiteSpace: "pre-wrap" }}>{value}</div>
    </div>
  );
}

// ── Compare row ───────────────────────────────────────────────────────────────

function CompareRow({ label, original: _original, yours, onEdit }: {
  label: string; original: string; yours: string; onEdit: (v: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [val, setVal] = useState(yours);
  return (
    <div style={{ marginBottom: 14 }}>
      <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: T.muted, marginBottom: 6, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span>{label}</span>
        {!editing && (
          <button onClick={() => { setEditing(true); setVal(yours); }}
            style={{ background: "none", border: "none", cursor: "pointer", color: T.gold, fontSize: 10, fontFamily: sans }}>
            ✏ Edit
          </button>
        )}
      </div>
      {editing ? (
        <div>
          <textarea value={val} onChange={e => setVal(e.target.value)} rows={3}
            style={{ width: "100%", fontSize: 11.5, border: `1px solid ${T.gold}`, borderRadius: 6, padding: "8px 10px", resize: "vertical", fontFamily: sans, outline: "none", boxSizing: "border-box", color: T.body }} />
          <div style={{ display: "flex", gap: 6, marginTop: 4 }}>
            <Btn size="sm" variant="primary" onClick={() => { onEdit(val); setEditing(false); }}>Save</Btn>
            <Btn size="sm" variant="ghost"   onClick={() => setEditing(false)}>Cancel</Btn>
          </div>
        </div>
      ) : (
        <div style={{ fontSize: 11.5, color: T.body, lineHeight: 1.6, padding: "8px 10px", background: T.bg, border: `1px solid ${T.line}`, borderRadius: 6 }}>
          {yours || "—"}
        </div>
      )}
    </div>
  );
}

function HighlightsCompare({ origRaw: _origRaw, yours, onChange }: {
  origRaw: string; yours: string[]; onChange: (v: string[]) => void;
}) {
  return (
    <div style={{ marginBottom: 14 }}>
      <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: T.muted, marginBottom: 6 }}>Highlights</div>
      <div style={{ padding: "8px 10px", background: T.bg, border: `1px solid ${T.line}`, borderRadius: 6 }}>
        {yours.map((h, i) => (
          <div key={i} style={{ display: "flex", gap: 4, marginBottom: 4, alignItems: "flex-start" }}>
            <span style={{ color: T.gold, fontWeight: 700, flexShrink: 0 }}>•</span>
            <input value={h} onChange={e => { const n = [...yours]; n[i] = e.target.value; onChange(n); }}
              style={{ flex: 1, fontSize: 11, border: `1px solid ${T.line}`, borderRadius: 4, padding: "2px 6px", fontFamily: sans, outline: "none", background: "transparent" }} />
            <button onClick={() => onChange(yours.filter((_, j) => j !== i))}
              style={{ background: "none", border: "none", cursor: "pointer", color: T.muted2, padding: 0, flexShrink: 0 }}>×</button>
          </div>
        ))}
        <button onClick={() => onChange([...yours, ""])}
          style={{ fontSize: 11, color: T.gold, background: "none", border: "none", cursor: "pointer", fontFamily: sans }}>+ Add</button>
      </div>
    </div>
  );
}

function ItineraryCompare({ orig: _orig, yours, expand, setExpand }: {
  orig: string; yours: string; expand: boolean; setExpand: (v: boolean) => void;
}) {
  return (
    <div style={{ marginBottom: 14 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
        <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: T.muted }}>Itinerary</div>
        <button onClick={() => setExpand(!expand)} style={{ fontSize: 11, color: T.gold, background: T.goldTint, border: `1px solid ${T.goldSoft}`, borderRadius: 4, padding: "2px 10px", cursor: "pointer", fontFamily: sans, fontWeight: 600 }}>
          {expand ? "▲ Collapse" : "▼ Expand"}
        </button>
      </div>
      <div style={{ fontSize: 11.5, color: T.body, lineHeight: 1.6, maxHeight: expand ? "none" : 100, overflow: expand ? "visible" : "hidden", whiteSpace: "pre-wrap", padding: "8px 10px", background: T.bg, border: `1px solid ${T.line}`, borderRadius: 6 }}>
        {yours || "—"}
      </div>
    </div>
  );
}
