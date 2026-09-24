"use client";

import React, { useState, useEffect, useRef, useMemo } from "react";
import { X, ChevronRight } from "lucide-react";
import { A, serif, sans, mono, Badge } from "./adminUi";

// ── Types ──────────────────────────────────────────────────────────────────────

export interface JudgeMeta {
  brand_fit: number | null;
  distinct: number | null;
  mission_present: boolean | null;
  feedback: string | null;
  judge_score: number | null;
}

export interface TourDetailFull {
  raw: {
    tour_id: string;
    src_name: string;
    src_subtitle: string | null;
    src_summary: string | null;
    src_description: string | null;
    src_highlights: string[] | null;
    src_itineraries: string | null;
    country: string | null;
    duration: string | null;
    price_raw: string | null;
    group_size: string | null;
    period: string | null;
    provider: string | null;
    inclusions: string | null;
    exclusions: string | null;
    pipeline_status: string;
    ingest_at: string | null;
  };
  generated: {
    id: string;
    version_num: number;
    created_at: string | null;
    status: string;
    aa_name: string;
    aa_subtitle: string | null;
    aa_summary: string | null;
    aa_description: string | null;
    aa_highlights: string[] | null;
    aa_itineraries: string | null;
    seo_title: string | null;
    seo_meta: string | null;
    seo_keywords_used: string[] | null;
    model_editorial: string | null;
    score_overall: number | null;
    score_brand: number | null;
    score_seo: number | null;
    score_structure: number | null;
    score_quality: number | null;
    brand_audit_status: string | null;
    judge: JudgeMeta | null;
  } | null;
  published: {
    id: string;
    aa_name: string;
    aa_subtitle: string | null;
    quality_score: number | null;
    published_at: string | null;
  } | null;
}

interface HistoryRow {
  id: string;
  version_num: number;
  created_at: string | null;
  status: string;
  model_editorial: string | null;
  brand_rules_version: number | null;
  brand_name: string | null;
  prompt_version: string | null;
  score_overall: number | null;
  score_brand: number | null;
  score_seo: number | null;
  score_structure: number | null;
  llm_model: string | null;
  cost_usd: number | null;
}

interface VersionContent {
  id: string;
  version_num: number;
  model_id: string | null;
  quality_score: number | null;
  created_at: string | null;
  aa_name: string | null;
  aa_subtitle: string | null;
  aa_summary: string | null;
  aa_highlights: string[];
  aa_itineraries: string | null;
  seo_title: string | null;
  seo_meta: string | null;
}

// ── Helpers ────────────────────────────────────────────────────────────────────

export function scoreColor(s: number | null | undefined): string {
  if (s == null) return A.muted2;
  if (s >= 9) return A.green;
  if (s >= 7) return A.amber;
  return A.red;
}

function relTime(iso: string | null): string {
  if (!iso) return "—";
  const diff = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}

function modelShort(m: string | null | undefined): string {
  if (!m) return "—";
  if (m.startsWith("gpt")) return m;
  return m.split(".").pop()?.replace(/-v\d+:\d+$/, "") ?? m;
}

function arrayToText(arr: string[] | null | undefined): string {
  return (arr || []).join("\n");
}
function textToArray(text: string): string[] {
  return text.split("\n").map(s => s.trim()).filter(Boolean);
}
function arrayToComma(arr: string[] | null | undefined): string {
  return (arr || []).join(", ");
}
function commaToArray(text: string): string[] {
  return text.split(",").map(s => s.trim()).filter(Boolean);
}

// ── Toast ─────────────────────────────────────────────────────────────────────

function Toast({ msg, type }: { msg: string; type: "success" | "error" }) {
  return (
    <div style={{
      position: "fixed", bottom: 24, right: 24, zIndex: 999,
      background: type === "success" ? A.green : A.red,
      color: "#fff", padding: "11px 18px", borderRadius: 8,
      fontSize: 13, fontWeight: 500, boxShadow: "0 4px 12px rgba(0,0,0,0.2)",
    }}>
      {msg}
    </div>
  );
}

// ── EditableField ─────────────────────────────────────────────────────────────

function EditableField({ label, initialValue, onSave, onChange, rows, charLimit, placeholder }: {
  label: string;
  initialValue: string;
  onSave: (v: string) => Promise<void>;
  onChange?: (v: string) => void;
  rows?: number;
  charLimit?: number;
  placeholder?: string;
}) {
  const [value, setValue] = useState(initialValue);
  const [state, setState] = useState<"idle" | "saving" | "saved" | "error">("idle");
  const lastSavedRef = useRef(initialValue);

  useEffect(() => {
    setValue(initialValue);
    lastSavedRef.current = initialValue;
  }, [initialValue]);

  async function handleBlur() {
    if (value === lastSavedRef.current || state === "saving") return;
    setState("saving");
    try {
      await onSave(value);
      lastSavedRef.current = value;
      setState("saved");
      setTimeout(() => setState("idle"), 2000);
    } catch {
      setState("error");
      setTimeout(() => setState("idle"), 3000);
    }
  }

  function handleChange(v: string) {
    setValue(v);
    onChange?.(v);
  }

  const inputStyle: React.CSSProperties = {
    width: "100%", padding: "7px 11px", borderRadius: 7,
    border: `1px solid ${state === "error" ? A.red : A.line}`,
    background: "#fff", fontSize: 13, color: A.ink, fontFamily: sans,
    boxSizing: "border-box",
    resize: rows ? "vertical" : "none",
    outline: "none",
    lineHeight: 1.5,
  };

  const statusText = state === "saving" ? "Saving…" : state === "saved" ? "✓ Saved" : state === "error" ? "Save failed" : "";
  const statusColor = state === "saving" ? A.amber : state === "saved" ? A.green : state === "error" ? A.red : "transparent";

  return (
    <div style={{ marginBottom: 14 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 4 }}>
        <span style={{ fontSize: 11, fontWeight: 600, color: A.muted, textTransform: "uppercase", letterSpacing: "0.1em" }}>
          {label}
        </span>
        <span style={{ fontSize: 11, color: statusColor }}>{statusText}</span>
      </div>
      {charLimit && (
        <div style={{ fontSize: 11, textAlign: "right", marginBottom: 3, color: value.length > charLimit * 0.9 ? A.amber : A.muted2 }}>
          {value.length}/{charLimit}
        </div>
      )}
      {rows ? (
        <textarea
          value={value}
          rows={rows}
          onChange={e => handleChange(e.target.value)}
          onBlur={handleBlur}
          placeholder={placeholder}
          style={inputStyle}
        />
      ) : (
        <input
          type="text"
          value={value}
          onChange={e => handleChange(e.target.value)}
          onBlur={handleBlur}
          placeholder={placeholder}
          style={inputStyle}
        />
      )}
    </div>
  );
}

// ── TourDetailPanelV2 ─────────────────────────────────────────────────────────

// ── SEO Keywords panel (AA-203) ─────────────────────────────────────────────

interface KeywordIdea {
  keyword: string;
  search_volume: number | null;
  competition: string | number | null;
  competition_index: number | null;
  cpc: number | null;
}

interface SeoContextResponse {
  has_data: boolean;
  seed: string | null;
  buyer_market: string | null;
  buyer_market_location_code: number | null;
  keyword_ideas: KeywordIdea[];
  top_keywords: string[];
  people_also_ask: string[];
  related_keywords: string[];
  fetched_at: string | null;
  notes?: string;
}

type SeoSortKey = "keyword" | "volume" | "competition" | "cpc";

function compNumeric(k: KeywordIdea): number {
  if (k.competition_index != null) return k.competition_index;
  if (typeof k.competition === "number") return k.competition;
  if (typeof k.competition === "string") {
    return ({ LOW: 1, MEDIUM: 2, HIGH: 3 } as Record<string, number>)[k.competition.toUpperCase()] ?? -1;
  }
  return -1;
}
function compDisplay(k: KeywordIdea): string {
  if (k.competition_index != null) return String(k.competition_index);
  if (k.competition != null) return String(k.competition);
  return "—";
}
function fmtVolume(v: number | null): string {
  return v == null ? "—" : v.toLocaleString("en-US");
}
function fmtCpc(v: number | null): string {
  return v == null ? "—" : `$${Number(v).toFixed(2)}`;
}

function SeoKeywordsPanel({ tourId }: { tourId: string }) {
  const [data, setData] = useState<SeoContextResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [sortKey, setSortKey] = useState<SeoSortKey>("volume");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError(false);
    fetch(`/api/admin/tours/${tourId}/seo-context`)
      .then(r => r.json())
      .then(d => { if (alive) setData(d); })
      .catch(() => { if (alive) setError(true); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [tourId]);

  function onSort(key: SeoSortKey) {
    if (key === sortKey) {
      setSortDir(d => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir(key === "keyword" ? "asc" : "desc");
    }
  }

  const sorted = useMemo(() => {
    const arr = [...(data?.keyword_ideas || [])];
    const dir = sortDir === "asc" ? 1 : -1;
    arr.sort((a, b) => {
      if (sortKey === "keyword") {
        const av = (a.keyword || "").toLowerCase();
        const bv = (b.keyword || "").toLowerCase();
        return av < bv ? -dir : av > bv ? dir : 0;
      }
      let av: number; let bv: number;
      if (sortKey === "volume") { av = a.search_volume ?? -1; bv = b.search_volume ?? -1; }
      else if (sortKey === "competition") { av = compNumeric(a); bv = compNumeric(b); }
      else { av = a.cpc ?? -1; bv = b.cpc ?? -1; }
      return (av - bv) * dir;
    });
    return arr;
  }, [data, sortKey, sortDir]);

  if (loading) {
    return <div style={{ textAlign: "center", padding: 40, color: A.muted }}>Loading SEO keywords…</div>;
  }
  if (error) {
    return <div style={{ textAlign: "center", padding: 40, color: A.red }}>Failed to load SEO data</div>;
  }
  if (!data || !data.has_data) {
    return (
      <div style={{ textAlign: "center", padding: 48, color: A.muted, fontSize: 13 }}>
        No DataForSEO data for this tour yet — run S1 with SEO mode enabled.
      </div>
    );
  }

  const arrow = (key: SeoSortKey) => (sortKey === key ? (sortDir === "asc" ? " ▲" : " ▼") : "");
  const th = (key: SeoSortKey, label: string, align: "left" | "right"): React.CSSProperties => ({
    padding: "8px 12px", textAlign: align, cursor: "pointer", userSelect: "none",
    fontSize: 11, fontWeight: 600, color: sortKey === key ? A.ink : A.muted,
    textTransform: "uppercase" as const, letterSpacing: "0.1em",
  });

  return (
    <div>
      {/* Header — seed + buyer market + fetched_at */}
      <div style={{
        display: "flex", gap: 16, flexWrap: "wrap", padding: "10px 14px",
        background: A.bg, borderRadius: 8, marginBottom: 18, fontSize: 12, color: A.muted,
      }}>
        <span>Seed: <strong style={{ color: A.ink }}>{data.seed || "—"}</strong></span>
        <span>
          Buyer market: <strong style={{ color: A.ink }}>{data.buyer_market || "—"}</strong>
          {data.buyer_market_location_code != null && (
            <span style={{ color: A.muted2 }}> ({data.buyer_market_location_code})</span>
          )}
        </span>
        <span>Fetched: <strong style={{ color: A.ink }}>{relTime(data.fetched_at)}</strong></span>
        <span>Ideas: <strong style={{ color: A.ink }}>{data.keyword_ideas.length}</strong></span>
      </div>

      {sorted.length === 0 ? (
        <div style={{ textAlign: "center", padding: 40, color: A.muted, fontSize: 13 }}>
          Row found but no keyword ideas stored.
        </div>
      ) : (
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
          <thead>
            <tr style={{ background: A.line2 }}>
              <th style={th("keyword", "Keyword", "left")} onClick={() => onSort("keyword")}>Keyword{arrow("keyword")}</th>
              <th style={th("volume", "Volume", "right")} onClick={() => onSort("volume")}>Volume{arrow("volume")}</th>
              <th style={th("competition", "Competition", "right")} onClick={() => onSort("competition")}>Competition{arrow("competition")}</th>
              <th style={th("cpc", "CPC", "right")} onClick={() => onSort("cpc")}>CPC{arrow("cpc")}</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((k, i) => (
              <tr key={`${k.keyword}-${i}`} style={{ borderTop: `1px solid ${A.line}`, background: i % 2 === 0 ? "#fff" : A.bg }}>
                <td style={{ padding: "8px 12px", color: A.ink }}>{k.keyword}</td>
                <td style={{ padding: "8px 12px", textAlign: "right", fontFamily: mono, color: A.body }}>{fmtVolume(k.search_volume)}</td>
                <td style={{ padding: "8px 12px", textAlign: "right", color: A.muted }}>{compDisplay(k)}</td>
                <td style={{ padding: "8px 12px", textAlign: "right", fontFamily: mono, color: A.gold }}>{fmtCpc(k.cpc)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

export function TourDetailPanelV2({ tourId, tourName, rewriteCount = 0, onClose }: {
  tourId: string;
  tourName: string;
  rewriteCount?: number;
  onClose: () => void;
}) {
  const [detail, setDetail] = useState<TourDetailFull | null>(null);
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState<"original" | "rewrite" | "history" | "seo" | "published">("original");
  const [history, setHistory] = useState<HistoryRow[]>([]);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [toast, setToast] = useState<{ msg: string; type: "success" | "error" } | null>(null);
  const [selectedVersion, setSelectedVersion] = useState<VersionContent | null>(null);
  const [loadingVersion, setLoadingVersion] = useState(false);
  const [promoting, setPromoting] = useState(false);
  const [rawDraft, setRawDraft] = useState<Record<string, string>>({});
  const [savingAll, setSavingAll] = useState(false);

  useEffect(() => {
    setLoading(true);
    setDetail(null);
    fetch(`/api/admin/tours/${tourId}/detail`)
      .then(r => r.json())
      .then(d => {
        setDetail(d);
        const r = d?.raw ?? {};
        setRawDraft({
          src_name: r.src_name ?? "",
          country: r.country ?? "",
          duration: r.duration ?? "",
          group_size: r.group_size ?? "",
          price_raw: r.price_raw ?? "",
          period: r.period ?? "",
          provider: r.provider ?? "",
          src_summary: r.src_summary ?? "",
          src_highlights: arrayToText(r.src_highlights),
          src_itineraries: r.src_itineraries ?? "",
          src_description: r.src_description ?? "",
          inclusions: r.inclusions ?? "",
          exclusions: r.exclusions ?? "",
        });
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [tourId]);

  useEffect(() => {
    if (tab !== "history") return;
    setLoadingHistory(true);
    fetch(`/api/admin/tours/${tourId}/history`)
      .then(r => r.json())
      .then(d => setHistory(d.history || []))
      .catch(() => {})
      .finally(() => setLoadingHistory(false));
  }, [tab, tourId]);

  function showToast(msg: string, type: "success" | "error") {
    setToast({ msg, type });
    setTimeout(() => setToast(null), 3000);
  }

  async function loadVersion(versionNum: number) {
    setLoadingVersion(true);
    setSelectedVersion(null);
    try {
      const r = await fetch(`/api/admin/tours/${tourId}/versions/${versionNum}`);
      if (r.ok) setSelectedVersion(await r.json());
    } finally { setLoadingVersion(false); }
  }

  async function promoteVersion(versionNum: number) {
    if (!confirm(`Make v${versionNum} the current published version for this tour?`)) return;
    setPromoting(true);
    try {
      const r = await fetch(`/api/admin/tours/${tourId}/versions/${versionNum}/promote`, { method: "POST" });
      if (r.ok) {
        showToast(`v${versionNum} is now the published version`, "success");
        setHistory(prev => prev.map(h => ({ ...h })));
      } else {
        showToast("Promote failed", "error");
      }
    } finally { setPromoting(false); }
  }

  async function saveRaw(field: string, value: string | string[]) {
    const res = await fetch(`/api/admin/tours/${tourId}/raw`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ [field]: value }),
    });
    if (!res.ok) { showToast("Save failed", "error"); throw new Error("Save failed"); }
    showToast("Saved", "success");
  }

  async function saveAllRaw() {
    setSavingAll(true);
    try {
      const body: Record<string, string | string[]> = { ...rawDraft };
      body.src_highlights = textToArray(rawDraft.src_highlights ?? "");
      const res = await fetch(`/api/admin/tours/${tourId}/raw`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) { showToast("Save failed", "error"); return; }
      showToast("All changes saved", "success");
    } finally { setSavingAll(false); }
  }

  async function saveGenerated(field: string, value: string | string[]) {
    const genId = detail?.generated?.id;
    if (!genId) { showToast("No generated content to save", "error"); throw new Error("No generated content"); }
    const res = await fetch(`/api/admin/tours/${tourId}/generated/${genId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ [field]: value }),
    });
    if (!res.ok) { showToast("Save failed", "error"); throw new Error("Save failed"); }
    showToast("Saved", "success");
  }

  const tabBtn = (active: boolean, disabled = false): React.CSSProperties => ({
    padding: "8px 16px", fontSize: 13,
    fontWeight: active ? 600 : 400,
    color: active ? A.ink : A.muted,
    cursor: disabled ? "default" : "pointer",
    background: "none", border: "none",
    borderBottom: active ? `2px solid ${A.gold}` : "2px solid transparent",
    fontFamily: sans,
    opacity: disabled ? 0.4 : 1,
  });

  const raw = detail?.raw;
  const gen = detail?.generated;
  const pub = detail?.published;

  return (
    <>
      {/* Backdrop */}
      <div
        onClick={onClose}
        style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.4)", zIndex: 199 }}
      />

      {/* Panel */}
      <div style={{
        position: "fixed", top: 0, right: 0, bottom: 0,
        width: "70vw", background: "#fff",
        boxShadow: "-4px 0 32px rgba(0,0,0,0.14)",
        zIndex: 200, display: "flex", flexDirection: "column",
        fontFamily: sans,
      }}>
        {/* Header */}
        <div style={{ padding: "16px 24px 0", borderBottom: `1px solid ${A.line}`, flexShrink: 0 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 10 }}>
            <div style={{ flex: 1, paddingRight: 12 }}>
              {/* Badges */}
              <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 6 }}>
                {raw?.country && <Badge color="blue">{raw.country}</Badge>}
                {raw?.pipeline_status && (
                  <Badge color={raw.pipeline_status === "published" ? "green" : "gray"}>
                    {raw.pipeline_status}
                  </Badge>
                )}
                {gen?.score_overall != null && (
                  <Badge color={gen.score_overall >= 9 ? "green" : gen.score_overall >= 7 ? "amber" : "red"}>
                    ★ {gen.score_overall.toFixed(1)}
                  </Badge>
                )}
              </div>
              {/* Name */}
              <div style={{ fontFamily: serif, fontSize: 17, fontWeight: 600, color: A.ink, lineHeight: 1.3, marginBottom: 5 }}>
                {gen?.aa_name || tourName}
              </div>
              {/* Meta */}
              <div style={{ fontSize: 11, color: A.muted, display: "flex", gap: 12, flexWrap: "wrap" }}>
                <span>ID: {tourId.slice(0, 8)}…</span>
                {rewriteCount > 0 && <span>Rewritten: {rewriteCount}×</span>}
                {pub?.published_at && <span>Published: {relTime(pub.published_at)}</span>}
              </div>
            </div>
            <button onClick={onClose} style={{ background: "none", border: "none", cursor: "pointer", color: A.muted, padding: 4 }}>
              <X size={18} />
            </button>
          </div>
          {/* Tabs */}
          <div style={{ display: "flex" }}>
            <button style={tabBtn(tab === "original")} onClick={() => setTab("original")}>Original Content</button>
            <button style={tabBtn(tab === "rewrite", !gen)} onClick={() => gen && setTab("rewrite")}>Latest Rewrite</button>
            <button style={tabBtn(tab === "history")} onClick={() => setTab("history")}>Rewrite History</button>
            <button style={tabBtn(tab === "seo")} onClick={() => setTab("seo")}>SEO Keywords</button>
            <button style={tabBtn(tab === "published", !pub)} onClick={() => pub && setTab("published")}>Published</button>
          </div>
        </div>

        {/* Body */}
        <div style={{ flex: 1, minHeight: 0, overflowY: "auto", padding: "20px 24px" }}>
          {loading ? (
            <div style={{ textAlign: "center", padding: 40, color: A.muted }}>Loading…</div>
          ) : !detail ? (
            <div style={{ textAlign: "center", padding: 40, color: A.red }}>Failed to load detail</div>
          ) : tab === "original" ? (
            <div>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0 24px" }}>
                <EditableField label="Tour Name" initialValue={raw?.src_name || ""} onSave={v => saveRaw("src_name", v)} onChange={v => setRawDraft(d => ({ ...d, src_name: v }))} />
                <EditableField label="Country" initialValue={raw?.country || ""} onSave={v => saveRaw("country", v)} onChange={v => setRawDraft(d => ({ ...d, country: v }))} />
                <EditableField label="Duration" initialValue={raw?.duration || ""} onSave={v => saveRaw("duration", v)} onChange={v => setRawDraft(d => ({ ...d, duration: v }))} />
                <EditableField label="Group Size" initialValue={raw?.group_size || ""} onSave={v => saveRaw("group_size", v)} onChange={v => setRawDraft(d => ({ ...d, group_size: v }))} />
                <EditableField label="Price" initialValue={raw?.price_raw || ""} onSave={v => saveRaw("price_raw", v)} onChange={v => setRawDraft(d => ({ ...d, price_raw: v }))} />
                <EditableField label="Period" initialValue={raw?.period || ""} onSave={v => saveRaw("period", v)} onChange={v => setRawDraft(d => ({ ...d, period: v }))} />
                <EditableField label="Provider" initialValue={raw?.provider || ""} onSave={v => saveRaw("provider", v)} onChange={v => setRawDraft(d => ({ ...d, provider: v }))} />
              </div>
              <EditableField label="Summary" initialValue={raw?.src_summary || ""} onSave={v => saveRaw("src_summary", v)} onChange={v => setRawDraft(d => ({ ...d, src_summary: v }))} rows={6} />
              <EditableField
                label="Highlights (one per line)"
                initialValue={arrayToText(raw?.src_highlights)}
                onSave={v => saveRaw("src_highlights", textToArray(v))}
                onChange={v => setRawDraft(d => ({ ...d, src_highlights: v }))}
                rows={4}
                placeholder="One highlight per line"
              />
              <EditableField label="Itineraries" initialValue={raw?.src_itineraries || ""} onSave={v => saveRaw("src_itineraries", v)} onChange={v => setRawDraft(d => ({ ...d, src_itineraries: v }))} rows={10} />
              <EditableField label="Description" initialValue={raw?.src_description || ""} onSave={v => saveRaw("src_description", v)} onChange={v => setRawDraft(d => ({ ...d, src_description: v }))} rows={6} />
              <EditableField label="Inclusions" initialValue={raw?.inclusions || ""} onSave={v => saveRaw("inclusions", v)} onChange={v => setRawDraft(d => ({ ...d, inclusions: v }))} rows={4} />
              <EditableField label="Exclusions" initialValue={raw?.exclusions || ""} onSave={v => saveRaw("exclusions", v)} onChange={v => setRawDraft(d => ({ ...d, exclusions: v }))} rows={4} />
              <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 8 }}>
                <button
                  onClick={saveAllRaw}
                  disabled={savingAll}
                  style={{
                    padding: "9px 22px", borderRadius: 7, border: "none", cursor: savingAll ? "not-allowed" : "pointer",
                    background: "#2563EB", color: "#fff", fontSize: 13, fontWeight: 600, opacity: savingAll ? 0.6 : 1,
                  }}
                >
                  {savingAll ? "Saving…" : "Save All"}
                </button>
              </div>
            </div>
          ) : tab === "rewrite" ? (
            gen ? (
              <div>
                {/* Info bar */}
                <div style={{
                  display: "flex", gap: 16, flexWrap: "wrap", padding: "10px 14px",
                  background: A.bg, borderRadius: 8, marginBottom: 20, fontSize: 12, color: A.muted,
                }}>
                  <span>Model: <strong>{modelShort(gen.model_editorial)}</strong></span>
                  <span>Score: <strong style={{ color: scoreColor(gen.score_overall) }}>{gen.score_overall?.toFixed(1) ?? "—"}</strong></span>
                  <span>Generated: <strong>{relTime(gen.created_at)}</strong></span>
                  <span>Version: <strong>v{gen.version_num}</strong></span>
                </div>
                {/* AA-209: full sub-score breakdown (incl. Quality) + judge-capped flag + brand judge */}
                {(() => {
                  const subs = [gen.score_brand, gen.score_seo, gen.score_structure, gen.score_quality];
                  const vAvg = subs.every(s => s != null)
                    ? (subs as number[]).reduce((a, s) => a + s, 0) / 4 : null;
                  const capped = gen.score_overall != null && vAvg != null && gen.score_overall < vAvg - 0.01;
                  const fmt = (s: number | null) => (s != null ? s.toFixed(1) : "—");
                  const rows: [string, number | null][] = [
                    ["Overall", gen.score_overall], ["Brand", gen.score_brand], ["SEO", gen.score_seo],
                    ["Structure", gen.score_structure], ["Quality", gen.score_quality],
                  ];
                  return (
                    <div style={{ marginBottom: 20 }}>
                      <div style={{ display: "flex", gap: 14, flexWrap: "wrap", marginBottom: capped ? 8 : 0 }}>
                        {rows.map(([label, sc]) => (
                          <span key={label} style={{ fontSize: 12, color: A.muted }}>
                            {label}: <strong style={{ color: scoreColor(sc) }}>{fmt(sc)}</strong>
                          </span>
                        ))}
                      </div>
                      {capped && <Badge color="amber">⚠ judge-capped (avg {fmt(vAvg)})</Badge>}
                      <div style={{ marginTop: 12, paddingTop: 12, borderTop: `1px solid ${A.line}` }}>
                        <div style={{ fontSize: 10, letterSpacing: "0.1em", textTransform: "uppercase", color: A.muted2, marginBottom: 6 }}>
                          Brand Judge
                        </div>
                        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                          {gen.judge ? (
                            <>
                              <Badge color="gold">fit {fmt(gen.judge.brand_fit)}</Badge>
                              <Badge color="gold">distinct {fmt(gen.judge.distinct)}</Badge>
                              <Badge color={gen.judge.mission_present ? "green" : "red"}>
                                mission {gen.judge.mission_present ? "✓" : "✗"}
                              </Badge>
                            </>
                          ) : (
                            <span style={{ fontSize: 12, color: A.muted2 }}>not run</span>
                          )}
                          {gen.brand_audit_status && (
                            <Badge color={gen.brand_audit_status === "pass" ? "green" : "amber"}>
                              {gen.brand_audit_status}
                            </Badge>
                          )}
                        </div>
                        {gen.judge?.feedback && (
                          <div style={{ fontSize: 12, color: A.body, lineHeight: 1.6, marginTop: 8 }}>
                            {gen.judge.feedback}
                          </div>
                        )}
                      </div>
                    </div>
                  );
                })()}
                <EditableField label="AA Name" initialValue={gen.aa_name || ""} onSave={v => saveGenerated("aa_name", v)} />
                <EditableField label="Subtitle" initialValue={gen.aa_subtitle || ""} onSave={v => saveGenerated("aa_subtitle", v)} />
                <EditableField label="Summary" initialValue={gen.aa_summary || ""} onSave={v => saveGenerated("aa_summary", v)} rows={6} />
                <EditableField
                  label="Highlights (one per line)"
                  initialValue={arrayToText(gen.aa_highlights)}
                  onSave={v => saveGenerated("aa_highlights", textToArray(v))}
                  rows={4}
                />
                <EditableField label="Itineraries" initialValue={gen.aa_itineraries || ""} onSave={v => saveGenerated("aa_itineraries", v)} rows={10} />
                <EditableField label="SEO Title" initialValue={gen.seo_title || ""} onSave={v => saveGenerated("seo_title", v)} charLimit={70} />
                <EditableField label="SEO Meta" initialValue={gen.seo_meta || ""} onSave={v => saveGenerated("seo_meta", v)} rows={3} charLimit={170} />
                <EditableField
                  label="Keywords (comma-separated)"
                  initialValue={arrayToComma(gen.seo_keywords_used)}
                  onSave={v => saveGenerated("seo_keywords_used", commaToArray(v))}
                  placeholder="keyword1, keyword2, …"
                />
              </div>
            ) : (
              <div style={{ textAlign: "center", padding: 40, color: A.muted }}>No rewrite data yet</div>
            )
          ) : tab === "history" ? (
            loadingHistory ? (
              <div style={{ textAlign: "center", padding: 40, color: A.muted }}>Loading history…</div>
            ) : history.length === 0 ? (
              <div style={{ textAlign: "center", padding: 40, color: A.muted, fontSize: 13 }}>No rewrites yet</div>
            ) : (
              <div>
                <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
                  <thead>
                    <tr style={{ background: A.line2 }}>
                      {["Version", "Model", "Prompt", "Brand v", "Overall", "Brand", "SEO", "Cost", "Date", ""].map(h => (
                        <th key={h} style={{
                          padding: "8px 12px", textAlign: "left" as const,
                          fontSize: 11, fontWeight: 600, color: A.muted,
                          textTransform: "uppercase" as const, letterSpacing: "0.1em",
                        }}>{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {history.map((h, i) => {
                      const isActive = selectedVersion?.version_num === h.version_num;
                      return (
                        <tr
                          key={h.id}
                          onClick={() => loadVersion(h.version_num)}
                          style={{
                            borderTop: `1px solid ${A.line}`,
                            background: isActive ? `${A.gold}18` : i % 2 === 0 ? "#fff" : A.bg,
                            cursor: "pointer",
                          }}
                        >
                          <td style={{ padding: "8px 12px" }}>
                            <span style={{ fontSize: 12, padding: "2px 8px", borderRadius: 10, background: isActive ? A.gold : A.goldTint, color: isActive ? "#fff" : A.gold, fontWeight: 600 }}>
                              v{h.version_num}
                            </span>
                          </td>
                          <td style={{ padding: "8px 12px", fontFamily: mono, fontSize: 11, color: A.muted }}>{modelShort(h.model_editorial)}</td>
                          {/* AA-318: prompt_version was already typed on HistoryRow but never
                              rendered — the exact "no way to observe/compare prompt over time"
                              gap this issue is about. Short hash (sha256[:8], AA-289) as a
                              mono pill; a version's own change vs. the row above/below it is
                              the actual regression signal an admin scans this column for. */}
                          <td style={{ padding: "8px 12px" }}>
                            {h.prompt_version ? (
                              <span style={{ fontFamily: mono, fontSize: 10.5, padding: "1px 6px", borderRadius: 8, background: A.line2, color: A.ink2 }} title={`prompt_version ${h.prompt_version}`}>
                                {h.prompt_version}
                              </span>
                            ) : <span style={{ color: A.muted2 }}>—</span>}
                          </td>
                          <td style={{ padding: "8px 12px", fontSize: 11, color: A.muted2 }}>
                            {h.brand_rules_version != null ? <span style={{ padding: "1px 6px", borderRadius: 8, background: A.goldTint, color: A.gold, fontWeight: 600, fontSize: 10 }}>{h.brand_name ? `${h.brand_name} · v${h.brand_rules_version}` : `v${h.brand_rules_version}`}</span> : "—"}
                          </td>
                          <td style={{ padding: "8px 12px", fontWeight: 700, color: scoreColor(h.score_overall) }}>{h.score_overall != null ? h.score_overall.toFixed(1) : "—"}</td>
                          <td style={{ padding: "8px 12px", color: scoreColor(h.score_brand) }}>{h.score_brand != null ? h.score_brand.toFixed(1) : "—"}</td>
                          <td style={{ padding: "8px 12px", color: scoreColor(h.score_seo) }}>{h.score_seo != null ? h.score_seo.toFixed(1) : "—"}</td>
                          <td style={{ padding: "8px 12px", color: A.gold }}>{h.cost_usd != null ? `$${h.cost_usd.toFixed(4)}` : "—"}</td>
                          <td style={{ padding: "8px 12px", color: A.muted2, fontSize: 12 }}>{relTime(h.created_at)}</td>
                          <td style={{ padding: "8px 12px" }}>
                            <div style={{ display: "flex", gap: 6 }}>
                              <button
                                onClick={e => { e.stopPropagation(); loadVersion(h.version_num); }}
                                disabled={loadingVersion}
                                style={{ padding: "3px 8px", fontSize: 11, border: `1px solid ${A.line}`, borderRadius: 4, background: "#fff", cursor: "pointer", color: A.muted, fontWeight: 600 }}
                              >
                                {loadingVersion && isActive ? "…" : "View content"}
                              </button>
                              <button
                                onClick={e => { e.stopPropagation(); promoteVersion(h.version_num); }}
                                disabled={promoting}
                                style={{ padding: "3px 8px", fontSize: 11, border: `1px solid ${A.gold}`, borderRadius: 4, background: A.goldTint, cursor: "pointer", color: A.gold, fontWeight: 600 }}
                              >
                                {promoting ? "…" : "Make Current"}
                              </button>
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>

                {/* Version content preview */}
                {loadingVersion && (
                  <div style={{ marginTop: 16, padding: 20, textAlign: "center", color: A.muted, fontSize: 13 }}>Loading version content…</div>
                )}
                {selectedVersion && !loadingVersion && (
                  <div style={{ marginTop: 20, padding: "16px 20px", background: A.bg, borderRadius: 8, border: `1px solid ${A.line}` }}>
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 14 }}>
                      <div style={{ fontSize: 12, fontWeight: 600, color: A.muted, textTransform: "uppercase", letterSpacing: "0.08em" }}>
                        v{selectedVersion.version_num} — {modelShort(selectedVersion.model_id)}
                        {selectedVersion.quality_score != null && (
                          <span style={{ marginLeft: 10, color: scoreColor(selectedVersion.quality_score) }}>
                            ★ {selectedVersion.quality_score.toFixed(1)}
                          </span>
                        )}
                      </div>
                      <button
                        onClick={() => promoteVersion(selectedVersion.version_num)}
                        disabled={promoting}
                        style={{ padding: "5px 12px", fontSize: 12, border: `1px solid ${A.gold}`, borderRadius: 6, background: A.gold, cursor: "pointer", color: "#fff", fontWeight: 600 }}
                      >
                        {promoting ? "Promoting…" : "Make Current"}
                      </button>
                    </div>
                    {[
                      { label: "Name",        val: selectedVersion.aa_name },
                      { label: "Subtitle",    val: selectedVersion.aa_subtitle },
                      { label: "Summary",     val: selectedVersion.aa_summary },
                      { label: "Itineraries", val: selectedVersion.aa_itineraries },
                      { label: "SEO Title",   val: selectedVersion.seo_title },
                      { label: "SEO Meta",    val: selectedVersion.seo_meta },
                    ].map(({ label, val }) => val ? (
                      <div key={label} style={{ marginBottom: 12 }}>
                        <div style={{ fontSize: 10, fontWeight: 600, color: A.muted, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 4 }}>{label}</div>
                        <div style={{ fontSize: 13, color: A.body, lineHeight: 1.6, whiteSpace: "pre-wrap" }}>{val}</div>
                      </div>
                    ) : null)}
                    {selectedVersion.aa_highlights.length > 0 && (
                      <div style={{ marginBottom: 12 }}>
                        <div style={{ fontSize: 10, fontWeight: 600, color: A.muted, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 4 }}>Highlights</div>
                        <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13, color: A.body }}>
                          {selectedVersion.aa_highlights.map((h, i) => <li key={i}>{h}</li>)}
                        </ul>
                      </div>
                    )}
                  </div>
                )}
              </div>
            )
          ) : tab === "seo" ? (
            <SeoKeywordsPanel tourId={tourId} />
          ) : (
            /* Published tab */
            pub ? (
              <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
                <div>
                  <div style={{ fontSize: 11, color: A.muted, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.1em", marginBottom: 6 }}>Published Name</div>
                  <div style={{ fontFamily: serif, fontSize: 18, fontWeight: 500, color: A.ink }}>{pub.aa_name}</div>
                </div>
                {pub.aa_subtitle && (
                  <div>
                    <div style={{ fontSize: 11, color: A.muted, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.1em", marginBottom: 4 }}>Subtitle</div>
                    <div style={{ fontSize: 14, fontStyle: "italic", color: A.body }}>{pub.aa_subtitle}</div>
                  </div>
                )}
                <div>
                  <div style={{ fontSize: 11, color: A.muted, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.1em", marginBottom: 4 }}>Quality Score</div>
                  <div style={{ fontFamily: mono, fontSize: 26, fontWeight: 700, color: scoreColor(pub.quality_score) }}>
                    {pub.quality_score?.toFixed(1) ?? "—"}
                  </div>
                </div>
                <div>
                  <div style={{ fontSize: 11, color: A.muted, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.1em", marginBottom: 4 }}>Published At</div>
                  <div style={{ fontSize: 13, color: A.body }}>{relTime(pub.published_at)}</div>
                </div>
                <div style={{ borderTop: `1px solid ${A.line}`, paddingTop: 16 }}>
                  <a
                    href={`/admin/s1-rewrite?tour_id=${tourId}`}
                    style={{
                      display: "inline-flex", alignItems: "center", gap: 6,
                      padding: "9px 18px", borderRadius: 8,
                      background: A.gold, border: `1px solid ${A.gold}`,
                      fontSize: 13, fontWeight: 600, color: "#fff", textDecoration: "none",
                    }}
                  >
                    Re-run Rewrite <ChevronRight size={13} />
                  </a>
                </div>
              </div>
            ) : (
              <div style={{ textAlign: "center", padding: 40, color: A.muted }}>Not yet published</div>
            )
          )}
        </div>
      </div>

      {toast && <Toast msg={toast.msg} type={toast.type} />}
    </>
  );
}
