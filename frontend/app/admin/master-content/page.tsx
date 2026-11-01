"use client";
// app/admin/master-content/page.tsx

import React, { Suspense, useState, useEffect, useCallback, useMemo } from "react";
import { useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { RefreshCw, ChevronRight, Download, X, Trash2, RotateCcw } from "lucide-react";
import {
  Button,
  PageFrame, PageFrameHeader, PageHeader, useFormat, useIsPhone, useAdminIdentity, useT, type Translator,
  Checkbox, Input, Select,
  K, alpha, serif, sans, mono, Eyebrow, Badge, LoadingScreen, KpiTile, Section,
  KX, type Tone,
  ButtonGroup, DataTable, Drawer, IconButton, LinkButton, type ColumnDef,
  Tabs,
} from "../../_kit";
import { can, staffErrorMessage } from "@/lib/rbac";
import { RBAC_MESSAGES } from "@/lib/rbac.messages";
import { M } from "./messages";

type T = Translator<typeof M.en>;
import { BarChart2, Star, DollarSign } from "lucide-react";
import { TourDetailPanelV2 } from "../_components/TourDetailPanelV2";
import { CompareModal } from "../_components/CompareModal";
import { RetireTourDialog, type RetireAction } from "../_components/RetireTourDialog";
import { Pagination } from "../_components/Pagination";
import { SCORE_BANDS } from "@/lib/quality-registry.generated";

const AA_INTERNAL_ID = "00000000-0000-0000-0000-000000000001";
const PAGE_SIZE = 20;

interface RewrittenTour {
  version_id: string;
  tour_id: string | null;
  tour_name: string;
  country: string;
  quality_score: number;
  version_number: number | null;
  status: string;
  master_status: string;
  created_at: string;
  pending_review_count?: number;   // AA-626: # of this tour's versions still stuck in review queue
}

interface Summary {
  total_rewrites: number;
  total_llm_cost_usd: number;
  // AA-718: new filter-aware / windowed fields from the backend.
  llm_cost_window_usd?: number | null;
  llm_cost_window_label?: string;
  catalog_total?: number;
  avg_quality?: number | null;
  api_calls_this_month: number;
  quota_pct: number;
  plan_name: string;
  member_since: string;
  tours_view: number | string;
  pipeline_note: string | null;
}

interface Pagination {
  page: number;
  page_size: number;
  total: number;
}

// AA-722: one bucket of the master pool's quality-score distribution, from
// GET /admin/metrics/library (score_distribution). Shown here instead of the
// retired Dashboard's Content Library tab.
interface ScoreBucket {
  range: string;
  count: number;
}

interface DetailsResponse {
  summary: Summary;
  rewritten_tours: RewrittenTour[];
  pagination?: Pagination;
  facets?: { countries?: string[] };  // AA-739: every catalog country, not only this page's
}

interface TourVersion {
  id: string;
  version_num: number;
  model_id: string | null;
  quality_score: number | null;          // = score_overall (gate)
  score_brand?: number | null;           // AA-220 (H1): validate sub-scores from list endpoint
  score_seo?: number | null;
  score_structure?: number | null;
  score_quality?: number | null;
  judge_score?: number | null;           // AA-220 (H1): metadata.judge.judge_score
  created_at: string | null;
  is_current: boolean;
  brand_audit_status: string | null;
  brand_audit_codes: string[];
  brand_audit_issues: string[];
  fix_pass_applied: boolean;
  fix_pass_fields: string[];
}

interface JudgeMeta {
  brand_fit: number | null;
  distinct: number | null;
  mission_present: boolean | null;
  feedback: string | null;
  judge_score: number | null;
}

interface KeywordIdea {
  keyword: string;
  search_volume: number | null;
  competition: string | null;
  competition_index: number | null;
  cpc: number | null;
}

interface VersionDetail {
  id: string;
  version_num: number;
  model_id: string | null;
  quality_score: number | null;
  score_brand: number | null;
  score_seo: number | null;
  score_structure: number | null;
  created_at: string | null;
  aa_name: string | null;
  aa_subtitle: string | null;
  aa_summary: string | null;
  aa_description: string | null;
  aa_highlights: string[];
  aa_itineraries: string | null;
  seo_title: string | null;
  seo_meta: string | null;
  brand_name: string | null;
  seo_mode: string | null;
  dataforseo_used: boolean;
  llm_cost_usd: number | null;
  top_keywords: string[];
  // Source/metadata fields
  country: string | null;
  duration: string | null;
  group_size: string | null;
  price_raw: string | null;
  period: string | null;
  provider: string | null;
  inclusions: string | null;
  exclusions: string | null;
  // Brand audit fields
  brand_audit_status: string | null;
  brand_audit_codes: string[];
  brand_audit_issues: string[];
  fix_pass_applied: boolean;
  fix_pass_fields: string[];
  // AA-219: judge / quality / failure / revalidate + full DFS per tour seed
  judge: JudgeMeta | null;
  score_quality: number | null;
  failure_codes: string[];
  revalidate_ran: boolean;
  revalidate_passed: boolean;
  keyword_ideas: KeywordIdea[];
  people_also_ask: string[];
  related_keywords: string[];
  seed: string | null;
}

function scoreColor(s: number | null | undefined): string {
  if (s == null) return K.muted2;
  if (s >= SCORE_BANDS.top) return K.success;
  if (s >= SCORE_BANDS.pass) return K.amber;
  return K.danger;
}

/** The kit tone of a score (the KpiTile value colour), same bands as scoreColor. */
function scoreTone(s: number | null | undefined): Tone | undefined {
  if (s == null) return undefined;
  if (s >= SCORE_BANDS.top) return "success";
  if (s >= SCORE_BANDS.pass) return "warning";
  return "danger";
}

function statusBadge(status: string, t: T) {
  const color: Tone = status === "published" ? "success" : status === "active" ? "info" : "neutral";
  const text = status === "published" ? t("statusPublished") : status === "active" ? t("statusActive") : status;
  return <Badge tone={color}>{text}</Badge>;
}

function masterStatusBadge(ms: string, t: T) {
  if (ms === "trashed") return (
    <span style={{ padding: "2px 7px", borderRadius: 10, background: K.dangerSoft, color: K.danger, fontSize: 11, fontWeight: 700 }}>
      {t("trashedBadge")}
    </span>
  );
  if (ms === "inactive") return (
    <span style={{ padding: "2px 7px", borderRadius: 10, background: K.neutralBg, color: K.neutralFg, fontSize: 11, fontWeight: 600 }}>
      {t("inactiveBadge")}
    </span>
  );
  return null;
}

function BrandAuditBadge({ status, fixPassApplied, codes }: { status: string | null; fixPassApplied: boolean; codes?: string[] }) {
  const t = useT(M);
  if (!status) return <span style={{ color: K.muted2, fontSize: 11 }}>—</span>;
  if (status === "pass") return (
    <span style={{ padding: "2px 7px", borderRadius: 10, background: K.successSoft, color: K.success, fontSize: 11, fontWeight: 600 }}>{t("auditPass")}</span>
  );
  if (status === "flagged" && fixPassApplied) return (
    <span title={codes?.join(", ")} style={{ padding: "2px 7px", borderRadius: 10, background: K.amberSoft, color: K.amber, fontSize: 11, fontWeight: 600, cursor: codes?.length ? "help" : "default" }}>{t("auditFixed")}</span>
  );
  if (status === "flagged") return (
    <span title={codes?.join(", ")} style={{ padding: "2px 7px", borderRadius: 10, background: K.dangerSoft, color: K.danger, fontSize: 11, fontWeight: 600, cursor: codes?.length ? "help" : "default" }}>{t("auditFlagged")}</span>
  );
  if (status === "manual_check") return (
    <span style={{ padding: "2px 7px", borderRadius: 10, background: K.amberSoft, color: K.amber, fontSize: 11, fontWeight: 600 }}>{t("auditManual")}</span>
  );
  return <span style={{ color: K.muted2, fontSize: 11 }}>—</span>;
}

// ── Score Bar ─────────────────────────────────────────────────────────────────

function ScoreBar({ label, value, tooltip }: { label: string; value: number | null; tooltip?: string }) {
  const pct = value != null ? Math.min(100, (value / 10) * 100) : 0;
  const color = scoreColor(value);
  return (
    <div style={{ marginBottom: 6 }}>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 2 }}>
        <span
          style={{ fontSize: 11, color: K.muted, cursor: tooltip ? "help" : undefined }}
          title={tooltip}
        >{label}</span>
        <span style={{ fontSize: 11, fontWeight: 700, color, fontFamily: mono }}>
          {value != null ? value.toFixed(1) : "—"}
        </span>
      </div>
      <div style={{ height: 4, background: K.line, borderRadius: 2 }}>
        <div style={{ height: 4, width: `${pct}%`, background: color, borderRadius: 2, transition: "width .3s" }} />
      </div>
    </div>
  );
}

function modelLabel(m: string | null | undefined): string {
  if (!m) return "—";
  if (m.startsWith("gpt")) return m;
  return m.split(".").pop()?.replace(/-v\d+:\d+$/, "") ?? m;
}

// ── AA-219: DataForSEO per-panel section (sortable ideas + PAA + related) ──────
function fmtVol(v: number | null): string { return v == null ? "—" : v.toLocaleString(); }
function fmtCpcVal(v: number | null): string { return v == null ? "—" : `$${v.toFixed(2)}`; }

type DfsSortKey = "keyword" | "volume" | "competition" | "cpc";

function DfsCompareSection({ seed, ideas: ideasRaw, paa: paaRaw, related: relatedRaw }: {
  seed: string | null; ideas: KeywordIdea[]; paa: string[]; related: string[];
}) {
  // AA-235: jsonb columns can arrive as a {seed:null} object (empty DataForSEO), not an array.
  // Guard before any spread/.length so [...ideas] never throws "o is not iterable".
  const ideas   = Array.isArray(ideasRaw)   ? ideasRaw   : [];
  const paa     = Array.isArray(paaRaw)     ? paaRaw     : [];
  const related = Array.isArray(relatedRaw) ? relatedRaw : [];
  const t = useT(M);
  const [sortKey, setSortKey] = useState<DfsSortKey>("volume");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  function onSort(k: DfsSortKey) {
    if (k === sortKey) setSortDir(d => (d === "asc" ? "desc" : "asc"));
    else { setSortKey(k); setSortDir(k === "keyword" ? "asc" : "desc"); }
  }
  const sorted = useMemo(() => {
    const arr = [...ideas];
    const dir = sortDir === "asc" ? 1 : -1;
    arr.sort((a, b) => {
      if (sortKey === "keyword") {
        const av = (a.keyword || "").toLowerCase(); const bv = (b.keyword || "").toLowerCase();
        return av < bv ? -dir : av > bv ? dir : 0;
      }
      let av: number; let bv: number;
      if (sortKey === "volume") { av = a.search_volume ?? -1; bv = b.search_volume ?? -1; }
      else if (sortKey === "competition") { av = a.competition_index ?? -1; bv = b.competition_index ?? -1; }
      else { av = a.cpc ?? -1; bv = b.cpc ?? -1; }
      return (av - bv) * dir;
    });
    return arr;
  }, [ideas, sortKey, sortDir]);

  if (ideas.length === 0 && paa.length === 0 && related.length === 0) return null;

  return (
    <div style={{ borderBottom: `1px solid ${K.line}` }}>
      <div style={{ padding: "5px 20px", background: K.line2, fontSize: 10, fontWeight: 700, textTransform: "uppercase" as const, letterSpacing: "0.12em", color: K.muted }}>
        DataForSEO <span style={{ textTransform: "none" as const, fontWeight: 400, color: K.muted2 }}>{t("sharedPerSeed")}</span>
      </div>
      <div style={{ padding: "8px 20px", fontSize: 12 }}>
        <div style={{ marginBottom: 6, color: K.muted }}>{t("seed")} <strong style={{ color: K.ink }}>{seed || "—"}</strong></div>
        {ideas.length > 0 && (
          <div style={{ marginBottom: 8 }}>
            {/* AA-1006: a kit DataTable on the panel's own sort (header click = onSort). */}
            <DataTable<KeywordIdea & { rowId: string }>
              tableId="master-dfs-ideas"
              data={sorted.map((k, i) => ({ ...k, rowId: `${k.keyword}-${i}` }))}
              getRowId={(k) => k.rowId}
              searchable={false}
              paginate={false}
              compact
              sorting={[{ id: sortKey, desc: sortDir === "desc" }]}
              onSortingChange={(next) => onSort((next[0]?.id ?? sortKey) as DfsSortKey)}
              manualSorting
              columns={[
                { id: "keyword", header: t("colKeyword"), cell: ({ row: { original: k } }) => <span style={{ color: K.ink }}>{k.keyword}</span> },
                { id: "volume", header: t("colVol"), meta: { align: "right" },
                  cell: ({ row: { original: k } }) => <span style={{ fontFamily: mono, color: K.body }}>{fmtVol(k.search_volume)}</span> },
                { id: "competition", header: t("colComp"), meta: { align: "right" }, cell: ({ row: { original: k } }) => (
                  <span style={{ color: K.muted }}>{(k.competition || "—")}{k.competition_index != null ? ` (${k.competition_index})` : ""}</span>
                ) },
                { id: "cpc", header: t("colCpc"), meta: { align: "right" },
                  cell: ({ row: { original: k } }) => <span style={{ fontFamily: mono, color: K.accent }}>{fmtCpcVal(k.cpc)}</span> },
              ]}
            />
          </div>
        )}
        {paa.length > 0 && (
          <div style={{ marginBottom: 6 }}>
            <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase" as const, letterSpacing: "0.08em", color: K.muted, marginBottom: 2 }}>{t("peopleAlsoAsk")}</div>
            {paa.map((q, i) => <div key={i} style={{ color: K.body, lineHeight: 1.5 }}>• {q}</div>)}
          </div>
        )}
        {related.length > 0 && (
          <div>
            <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase" as const, letterSpacing: "0.08em", color: K.muted, marginBottom: 2 }}>{t("relatedKeywords")}</div>
            <div style={{ color: K.body, lineHeight: 1.6 }}>{related.join(", ")}</div>
          </div>
        )}
      </div>
    </div>
  );
}

// ── Version Compare Modal (full-screen, 2-4 panels) ───────────────────────────

interface PanelState {
  versionNum: number;
  data: VersionDetail | null;
  loading: boolean;
}

const PANEL_COLORS = [K.accent, K.info, K.success, K.purple];
// AA-1006: each compare panel is tinted with its own colour (were four fixed panel-bg tokens).
const PANEL_BG     = PANEL_COLORS.map((c) => alpha(c, 6));

// [field, its label key in messages.ts]
const CONTENT_FIELDS: [keyof VersionDetail, keyof typeof M.en][] = [
  ["aa_name",        "fTourName"],
  ["aa_subtitle",    "fSubtitle"],
  ["aa_summary",     "fSummary"],
  ["aa_highlights",  "fHighlights"],
  ["aa_itineraries", "fItineraries"],
  ["seo_title",      "fSeoTitle"],
  ["seo_meta",       "fSeoMeta"],
  ["aa_description", "fDescription"],
  ["top_keywords",   "fKeywords"],
  ["country",        "fCountry"],
  ["duration",       "fDuration"],
  ["group_size",     "fGroupSize"],
  ["price_raw",      "fPrice"],
  ["period",         "fPeriod"],
  ["provider",       "fProvider"],
  ["inclusions",     "fInclusions"],
  ["exclusions",     "fExclusions"],
];

// AA-220 (C): page-scope blob-download helper (pure) — shared by the compare modal and the
// Rewrite History rows. No React state here; callers that want a spinner wrap it themselves.
async function downloadBlob(url: string, filename: string) {
  const r = await fetch(url);
  if (!r.ok) return;
  const blob = await r.blob();
  const objUrl = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = objUrl; a.download = filename; a.click();
  URL.revokeObjectURL(objUrl);
}

// AA-220 (B/C): single-version DOCX export — page-scope so both the compare modal and the
// Rewrite History row can trigger it.
function exportVersionDocx(tourId: string, vnum: number) {
  if (vnum <= 0) return;
  const short = tourId.split("-")[0];
  downloadBlob(
    `/api/admin/tours/${tourId}/versions/${vnum}/export-docx`,
    `tour_${short}_v${vnum}.docx`,
  );
}

function VersionCompareModal({ tourId, tourName, versionNums, onClose }: {
  tourId: string;
  tourName: string;
  versionNums: [number, number];
  onClose: () => void;
}) {
  const t = useT(M);
  const { formatDate } = useFormat();
  const [panels, setPanels] = useState<PanelState[]>(
    versionNums.map(n => ({ versionNum: n, data: null, loading: true }))
  );
  const [allVersions, setAllVersions]   = useState<TourVersion[]>([]);
  const [error, setError]               = useState("");
  const [exporting, setExporting]       = useState(false);

  // AA-220 (A/C): horizontal multi-version comparison export of the panels currently shown.
  // Wraps the page-scope downloadBlob with the modal's local `exporting` spinner state.
  async function exportComparison(format: "csv" | "xlsx") {
    const vnums = panels.map(p => Number(p.versionNum)).filter(v => v > 0);
    if (vnums.length === 0) return;
    const short = tourId.split("-")[0];
    setExporting(true);
    try {
      await downloadBlob(
        `/api/admin/tours/${tourId}/versions/export?versions=${vnums.join(",")}&format=${format}`,
        `tour_${short}_versions_compare.${format}`,
      );
    } finally { setExporting(false); }
  }

  useEffect(() => {
    fetch(`/api/admin/tours/${tourId}/versions`)
      .then(r => r.ok ? r.json() : null)
      .then(d => { if (d) setAllVersions(d.versions ?? []); })
      .catch(() => {});
  }, [tourId]);

  // Fetch data for any panel that is still loading (loading: true means data needed)
  useEffect(() => {
    panels.forEach((panel, idx) => {
      if (!panel.loading) return;
      const vnum = Number(panel.versionNum);
      const fetchUrl = vnum === -1
        ? `/api/admin/tours/${tourId}/source`
        : `/api/admin/tours/${tourId}/versions/${vnum}`;
      fetch(fetchUrl)
        .then(r => r.ok ? r.json() : Promise.reject(`HTTP ${r.status}`))
        .then(data => {
          setPanels(prev => prev.map((p, i) =>
            i === idx && p.versionNum === panel.versionNum ? { ...p, data, loading: false } : p
          ));
        })
        .catch(e => {
          setError(String(e));
          setPanels(prev => prev.map((p, i) =>
            i === idx && p.versionNum === panel.versionNum ? { ...p, data: null, loading: false } : p
          ));
        });
    });
  }, [panels.map(p => `${p.versionNum}:${p.loading}`).join(",")]); // eslint-disable-line react-hooks/exhaustive-deps

  function changePanel(idx: number, vNum: number) {
    setPanels(prev => prev.map((p, i) =>
      i === idx ? { versionNum: vNum, data: null, loading: true } : p
    ));
  }

  function addPanel() {
    if (panels.length >= 4 || allVersions.length === 0) return;
    const used = new Set(panels.map(p => p.versionNum));
    const nextVNum = allVersions.find(v => !used.has(v.version_num))?.version_num ?? allVersions[0].version_num;
    setPanels(prev => [...prev, { versionNum: nextVNum, data: null, loading: true }]);
  }

  function removePanel(idx: number) {
    if (panels.length <= 2) return;
    setPanels(prev => prev.filter((_, i) => i !== idx));
  }

  function fieldSerial(v: VersionDetail | null, key: keyof VersionDetail): string {
    if (!v) return "\x00";
    const raw = v[key];
    if (raw == null) return "";
    if (Array.isArray(raw)) return JSON.stringify(raw);
    return String(raw);
  }

  function isFieldDiff(idx: number, key: keyof VersionDetail): boolean {
    if (idx === 0 || panels.length < 2) return false;
    if (!panels[0].data || !panels[idx].data) return false;
    return fieldSerial(panels[0].data, key) !== fieldSerial(panels[idx].data, key);
  }

  function cellVal(panel: PanelState, key: keyof VersionDetail): React.ReactNode {
    if (panel.loading) return <div style={{ padding: "10px 20px", fontSize: 12, color: K.muted2 }}>{t("loading")}</div>;
    const v = panel.data;
    if (!v) return <div style={{ padding: "10px 20px", fontSize: 12, color: K.muted2 }}>—</div>;
    const raw: unknown = v[key];
    let display: React.ReactNode;

    if ((key === "aa_highlights" || key === "top_keywords") && Array.isArray(raw)) {
      display = (
        <ul style={{ margin: 0, paddingLeft: 18, lineHeight: 1.7 }}>
          {(raw as string[]).map((h, i) => <li key={i}>{h}</li>)}
        </ul>
      );
    } else if (key === "seo_title") {
      const len = typeof raw === "string" ? raw.length : 0;
      display = <><span>{(raw as string) || "—"}</span>{typeof raw === "string" && <span style={{ marginLeft: 6, fontSize: 10, color: K.muted2 }}>{t("chars", len)}</span>}</>;
    } else if (key === "seo_meta") {
      const len = typeof raw === "string" ? raw.length : 0;
      const over = len > 170;
      display = <><span style={{ color: over ? K.danger : "inherit" }}>{(raw as string) || "—"}</span>{typeof raw === "string" && <span style={{ marginLeft: 6, fontSize: 10, color: over ? K.danger : K.muted2 }}>{t("chars", len)}{over ? " (>170)" : ""}</span>}</>;
    } else {
      display = <span>{typeof raw === "string" ? raw : raw == null ? "—" : String(raw)}</span>;
    }

    return (
      <div style={{ padding: "10px 20px", fontSize: 13, color: raw ? K.body : K.muted2, lineHeight: 1.6, whiteSpace: "pre-wrap" }}>
        {display}
      </div>
    );
  }

  const n = panels.length;

  return (
    <div style={{
      position: "fixed", top: 0, left: 240, width: "calc(100vw - 240px)", height: "100vh",
      background: K.card, zIndex: 300, display: "flex", flexDirection: "column",
      boxShadow: KX.shadowLg,
    }}>
      {/* Modal header */}
      <div style={{
        padding: "14px 24px", borderBottom: `1px solid ${K.line}`,
        display: "flex", alignItems: "center", justifyContent: "space-between",
        flexShrink: 0, background: K.card, zIndex: 10,
      }}>
        <div>
          <div style={{ fontFamily: serif, fontSize: 16, fontWeight: 500, color: K.ink }}>
            {t("compareTitle", tourName)}
          </div>
          <div style={{ fontSize: 12, color: K.muted, marginTop: 2 }}>
            {t("compareHint", n)}
          </div>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <Button size="sm" variant="ghost" onClick={() => exportComparison("xlsx")} disabled={exporting}>
            {exporting ? "…" : t("exportComparisonXlsx")}
          </Button>
          <Button size="sm" variant="ghost" onClick={() => exportComparison("csv")} disabled={exporting}>
            CSV
          </Button>
          {n < 4 && (
            <Button size="sm" variant="dashed" onClick={addPanel} disabled={allVersions.length === 0}>
              {t("addPanel")}
            </Button>
          )}
          <IconButton variant="plain" size="lg" onClick={onClose} label={t("close")}>
            <X size={20} />
          </IconButton>
        </div>
      </div>

      {error && (
        <div style={{ padding: "8px 24px", background: K.dangerSoft, color: K.danger, fontSize: 12, flexShrink: 0 }}>
          {error}
        </div>
      )}

      {/* Panels grid — each panel scrolls independently */}
      <div style={{ flex: 1, display: "grid", gridTemplateColumns: `repeat(${n}, 1fr)`, overflow: "hidden" }}>
        {panels.map((panel, idx) => {
          const v         = panel.data;
          const color     = PANEL_COLORS[idx] ?? PANEL_COLORS[0];
          const panelBg   = PANEL_BG[idx] ?? PANEL_BG[0];
          return (
            <div key={idx} style={{
              display: "flex", flexDirection: "column", overflow: "hidden",
              borderRight: idx < n - 1 ? `1px solid ${K.line}` : undefined,
            }}>
              <div style={{ overflowY: "auto", flex: 1, minHeight: 0 }}>
                {/* Sticky column header */}
                <div style={{ padding: "12px 20px", background: panelBg, position: "sticky", top: 0, zIndex: 5, borderBottom: `1px solid ${K.line}` }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
                    <Select
                      value={panel.versionNum}
                      onChange={e => changePanel(idx, parseInt(e.target.value))}
                      style={{
                        padding: "3px 8px", fontSize: 12, fontWeight: 700,
                        background: color, color: KX.onSolid,
                        border: "none", borderRadius: 10, cursor: "pointer",
                      }}
                    >
                      {idx === 0 && <option value={-1}>{t("original")}</option>}
                      {allVersions.map(av => (
                        <option key={av.version_num} value={av.version_num}>v{av.version_num}</option>
                      ))}
                    </Select>
                    {v && (
                      <span style={{ fontSize: 12, color: K.muted }}>{modelLabel(v.model_id)}</span>
                    )}
                    {Number(panel.versionNum) > 0 && (
                      <Button size="sm" variant="ghost" onClick={() => exportVersionDocx(tourId, Number(panel.versionNum))}
                        disabled={exporting} title={t("exportDocx")} style={{ padding: "2px 8px" }}>
                        DOCX
                      </Button>
                    )}
                    {v?.created_at && (
                      <span style={{ fontSize: 11, color: K.muted2, marginLeft: "auto" }}>{formatDate(v.created_at)}</span>
                    )}
                    {n > 2 && (
                      <IconButton variant="plain" size="xs" onClick={() => removePanel(idx)} label={t("removePanel")}
                        style={{ marginLeft: v?.created_at ? 0 : "auto" }}>
                        <X size={14} />
                      </IconButton>
                    )}
                  </div>

                  {/* Score bars */}
                  {v ? (
                    <div style={{ padding: "8px 0", borderTop: `1px solid ${K.line}`, marginTop: 4 }}>
                      <ScoreBar label={t("overallGate")} value={v.quality_score} tooltip={t("overallGateTip")} />
                      <ScoreBar label={t("brand")} value={v.score_brand} />
                      <ScoreBar label={t("seo")} value={v.score_seo} />
                      <ScoreBar label={t("structure")} value={v.score_structure} />
                      <ScoreBar label={t("qualityValidate")} value={v.score_quality} tooltip={t("qualityValidateTip")} />
                      {/* AA-220 (G2): clarify gate vs sub-score so they're not read as the same metric */}
                      <div style={{ fontSize: 10, color: K.muted2, lineHeight: 1.5, marginTop: 4 }}>
                        {t("gateNote")}
                      </div>
                    </div>
                  ) : (
                    <div style={{ fontSize: 12, color: K.muted2, padding: "8px 0" }}>
                      {panel.loading ? t("loading") : t("noData")}
                    </div>
                  )}

                  {/* Rewrite config */}
                  {v && (
                    <div style={{ padding: "8px 0", borderTop: `1px solid ${K.line}`, marginTop: 4, fontSize: 11 }}>
                      <div style={{ fontWeight: 600, color: K.muted, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 4 }}>{t("rewriteConfig")}</div>
                      <div style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: "3px 8px", color: K.body }}>
                        <span style={{ color: K.muted }}>{t("model")}</span>
                        <span style={{ fontFamily: mono }}>{modelLabel(v.model_id)}</span>
                        <span style={{ color: K.muted }}>{t("brand")}</span>
                        <span>{v.brand_name || t("brandDefault")}</span>
                        <span style={{ color: K.muted }}>{t("seoMode")}</span>
                        <span>{v.seo_mode || t("seoStandard")}</span>
                        <span style={{ color: K.muted }}>DataForSEO</span>
                        <span style={{ color: v.dataforseo_used ? K.success : K.muted2 }}>{v.dataforseo_used ? t("live") : t("mock")}</span>
                        <span style={{ color: K.muted }}>{t("cost")}</span>
                        <span style={{ fontFamily: mono }}>{v.llm_cost_usd != null ? `$${v.llm_cost_usd.toFixed(4)}` : "—"}</span>
                      </div>
                    </div>
                  )}

                  {/* Brand Audit */}
                  {v && v.brand_audit_status && (
                    <div style={{ padding: "8px 0", borderTop: `1px solid ${K.line}`, marginTop: 4, fontSize: 11 }}>
                      <div style={{ fontWeight: 600, color: K.muted, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 4 }}>{t("brandAudit")}</div>
                      <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 4 }}>
                        <BrandAuditBadge
                          status={v.brand_audit_status}
                          fixPassApplied={v.fix_pass_applied ?? false}
                          codes={v.brand_audit_codes}
                        />
                      </div>
                      {v.brand_audit_codes && v.brand_audit_codes.length > 0 && (
                        <div style={{ color: K.muted, lineHeight: 1.6 }}>
                          {v.brand_audit_codes.map(code => (
                            <div key={code} style={{ fontFamily: mono }}>• {code}</div>
                          ))}
                        </div>
                      )}
                      {v.fix_pass_applied && v.fix_pass_fields && v.fix_pass_fields.length > 0 && (
                        <div style={{ color: K.info, marginTop: 4 }}>
                          {t("fixed", v.fix_pass_fields.join(", "))}
                        </div>
                      )}
                    </div>
                  )}

                  {/* AA-219: Judge (metadata.judge) */}
                  {v && v.judge && (
                    <div style={{ padding: "8px 0", borderTop: `1px solid ${K.line}`, marginTop: 4, fontSize: 11 }}>
                      <div style={{ fontWeight: 600, color: K.muted, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 4 }}>{t("judge")}</div>
                      <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginBottom: 4 }}>
                        {v.judge.brand_fit != null && <Badge tone="accent">{t("judgeFit", v.judge.brand_fit.toFixed(1))}</Badge>}
                        {v.judge.distinct != null && <Badge tone="accent">{t("judgeDistinct", v.judge.distinct.toFixed(1))}</Badge>}
                        {v.judge.mission_present != null && (
                          <Badge tone={v.judge.mission_present ? "success" : "danger"}>
                            {v.judge.mission_present ? t("missionYes") : t("missionNo")}
                          </Badge>
                        )}
                        {v.judge.judge_score != null && <Badge tone="info">{t("judgeScore", v.judge.judge_score.toFixed(1))}</Badge>}
                      </div>
                      {v.judge.feedback && <div style={{ color: K.body, lineHeight: 1.5 }}>{v.judge.feedback}</div>}
                    </div>
                  )}

                  {/* AA-219: failure codes + revalidate */}
                  {v && (v.failure_codes.length > 0 || v.revalidate_ran) && (
                    <div style={{ padding: "8px 0", borderTop: `1px solid ${K.line}`, marginTop: 4, fontSize: 11 }}>
                      <div style={{ fontWeight: 600, color: K.muted, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 4 }}>{t("validation")}</div>
                      {v.failure_codes.length > 0 && (
                        <div style={{ color: K.danger, lineHeight: 1.6, marginBottom: 4 }}>
                          {v.failure_codes.map(c => <div key={c} style={{ fontFamily: mono }}>• {c}</div>)}
                        </div>
                      )}
                      {v.revalidate_ran ? (
                        <Badge tone={v.revalidate_passed ? "success" : "danger"}>
                          {v.revalidate_passed ? t("revalidated") : t("revalFailed")}
                        </Badge>
                      ) : (
                        <span style={{ color: K.muted2 }}>—</span>
                      )}
                    </div>
                  )}
                </div>

                {/* Keywords */}
                {v && v.top_keywords.length > 0 && (
                  <div style={{ borderBottom: `1px solid ${K.line}` }}>
                    <div style={{ padding: "5px 20px", background: K.line2, fontSize: 10, fontWeight: 700, textTransform: "uppercase" as const, letterSpacing: "0.12em", color: K.muted }}>
                      {t("keywords")}
                    </div>
                    <div style={{ padding: "8px 20px", fontSize: 12, color: K.body }}>
                      {v.top_keywords.slice(0, 15).join(", ")}
                    </div>
                  </div>
                )}

                {/* AA-219: full DataForSEO (ideas table + PAA + related) per tour seed */}
                {v && (
                  <DfsCompareSection
                    seed={v.seed}
                    ideas={Array.isArray(v.keyword_ideas) ? v.keyword_ideas : []}
                    paa={Array.isArray(v.people_also_ask) ? v.people_also_ask : []}
                    related={Array.isArray(v.related_keywords) ? v.related_keywords : []}
                  />
                )}

                {/* Content fields with diff highlight */}
                {CONTENT_FIELDS.map(([key, label]) => {
                  const diff = isFieldDiff(idx, key);
                  return (
                    <div key={key} style={{ borderBottom: `1px solid ${K.line}`, background: diff ? K.amberSoft : "transparent" }}>
                      <div style={{
                        padding: "5px 20px",
                        background: diff ? K.amberSoft : K.line2,
                        fontSize: 10, fontWeight: 700,
                        textTransform: "uppercase" as const, letterSpacing: "0.12em", color: K.muted,
                        display: "flex", alignItems: "center", gap: 6,
                      }}>
                        {t(label)}
                        {diff && <span style={{ color: K.amber, fontSize: 9 }}>{t("diff")}</span>}
                      </div>
                      {cellVal(panel, key)}
                    </div>
                  );
                })}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── Main page ─────────────────────────────────────────────────────────────────

export default function MasterContentPage() {
  // useSearchParams() needs a Suspense boundary in the App Router.
  return (
    <Suspense fallback={null}>
      <MasterContentPageInner />
    </Suspense>
  );
}

function MasterContentPageInner() {
  // AA-882 — the master lifecycle (activate / deactivate / trash / restore) is master.admin on the
  // backend; other roles see the catalog without those buttons.
  const role = useAdminIdentity().role;
  const t = useT(M);
  const tr = t; // the row loops below name the tour `t`
  const rbacT = useT(RBAC_MESSAGES);
  const { formatDate } = useFormat();
  const canLifecycle = can(role, "master.admin");
  // AA-878 — promoting a version is content.write: the read-only viewer sees the versions without the button.
  const canPromote = can(role, "content.write");
  // AA-752 — deep link from the ⌘K palette: /admin/master-content?tour=<tour_id> opens that tour's
  // detail panel. Read the param once with a lazy useState initializer (no setState-in-effect).
  const searchParams = useSearchParams();
  const [search, setSearch]             = useState("");
  const [detailTourId, setDetailTourId] = useState<string | null>(() => searchParams.get("tour"));
  const [detailTourName, setDetailTourName] = useState("");
  const [selectedIds, setSelectedIds]   = useState<Set<string>>(new Set());
  const [compareOpen, setCompareOpen]   = useState(false);
  const isPhone = useIsPhone();
  const [expandedTours, setExpandedTours] = useState<Set<string>>(new Set());
  // AA-1006: the tour whose versions are open in the Drawer (the table row used to expand in place).
  const [versionsTour, setVersionsTour] = useState<{ tourId: string; tourName: string } | null>(null);
  const [tourVersions, setTourVersions] = useState<Record<string, TourVersion[]>>({});
  const [versionLoading, setVersionLoading] = useState<Record<string, boolean>>({});
  const [promoting, setPromoting]       = useState<string | null>(null);
  const [page, setPage]                 = useState(1);
  const [pageSize, setPageSize]         = useState(PAGE_SIZE);  // AA-739: user-selectable
  // AA-718: server-side sort (whitelisted on the backend).
  const [sortKey, setSortKey]           = useState<"tour_name" | "country" | "quality_score" | "created_at" | "master_status">("created_at");
  const [sortDir, setSortDir]           = useState<"asc" | "desc">("desc");
  const [countryFilter, setCountryFilter] = useState("");
  const [scoreFilter, setScoreFilter]   = useState("");
  const [versionFilter, setVersionFilter] = useState("");
  const [exporting, setExporting]       = useState(false);
  const [trashing, setTrashing]         = useState<string | null>(null);
  // AA-986 L2 (Q8) — retire / trash go through a confirm dialog with a dry run of what they affect.
  const [retireTarget, setRetireTarget] = useState<{ id: string; name: string; action: RetireAction } | null>(null);
  const [restoring, setRestoring]       = useState<string | null>(null);
  const [toggling, setToggling]         = useState<string | null>(null);
  const [masterStatusFilter, setMasterStatusFilter] = useState<string>("");
  const [toast, setToast]               = useState("");

  const [compareVersionSel, setCompareVersionSel] = useState<Record<string, Set<number>>>({});
  const [compareVersionOpen, setCompareVersionOpen] = useState<{ tourId: string; tourName: string; vNums: [number, number] } | null>(null);

  // AA-718: all filtering/sort/paging is now server-side (bug 1/4/5 — the old client-side filter
  // over a LIMIT 200 list dropped the newest rows and made Total/Avg cap at 200). react-query
  // (AA-662) owns the fetch; the query key carries the filter/sort/page state so changing any of
  // them refetches the matching page + a filter-aware summary + pagination.total. No fetch-in-
  // effect (React Compiler rule, frontend/AGENTS.md).
  const { data, isLoading, isFetching, error: qError, refetch } = useQuery({
    queryKey: ["master-content", page, pageSize, sortKey, sortDir, search, countryFilter, scoreFilter, versionFilter, masterStatusFilter],
    queryFn: async () => {
      const qs = new URLSearchParams({
        page: String(page),
        page_size: String(pageSize),
        sort: sortKey,
        sort_dir: sortDir,
      });
      if (search.trim()) qs.set("search", search.trim());
      if (countryFilter) qs.set("country", countryFilter);
      if (scoreFilter) qs.set("score", scoreFilter);
      if (masterStatusFilter) qs.set("master_status", masterStatusFilter);
      if (versionFilter) qs.set("version", versionFilter);
      const res = await fetch(`/api/tenant/admin/tenants/${AA_INTERNAL_ID}/details?${qs.toString()}`);
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      return (await res.json()) as DetailsResponse;
    },
    placeholderData: (prev) => prev,  // keep the current page visible while the next loads
  });
  const loading = isLoading;
  const refreshing = isFetching && !isLoading;
  const error = qError ? (qError instanceof Error ? qError.message : t("loadFailed")) : "";

  // AA-722: the retired Dashboard's "Content Library" tab showed the master pool's score
  // distribution (GET /admin/metrics/library → score_distribution). It now lives here, replacing
  // the stale "Pipeline Runs" StatCard + "Recent Pipeline Runs" table (both fed by
  // shared.pipeline_runs, which only S0 upload writes). react-query (AA-662); no fetch-in-effect.
  const { data: libraryMetrics } = useQuery({
    queryKey: ["master-content-library"],
    queryFn: async () => {
      const res = await fetch("/api/admin/metrics/library");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return (await res.json()) as { score_distribution?: ScoreBucket[] };
    },
    staleTime: 60_000,
  });

  function refresh() { refetch(); }

  function toggleSort(key: typeof sortKey) {
    if (sortKey === key) setSortDir(d => (d === "asc" ? "desc" : "asc"));
    else { setSortKey(key); setSortDir(key === "quality_score" ? "desc" : "asc"); }
    setPage(1);
  }

  const tours = data?.rewritten_tours ?? [];
  const summary = data?.summary;
  const serverTotal = data?.pagination?.total ?? tours.length;

  // AA-722: master-pool score distribution (replaces the stale pipeline_runs widgets). Empty while
  // loading or when the metrics endpoint has no data yet.
  const scoreDistribution = libraryMetrics?.score_distribution ?? [];
  const scoreDistMax = scoreDistribution.reduce((m, b) => Math.max(m, b.count), 0);

  // AA-718/AA-739: every filter (search, country, score, version, master_status), sort and paging
  // runs on the server. The old "Status" select only ever matched "published" on this catalog and
  // "Version" filtered the current page only — both removed/moved server-side.
  const filtered = tours;

  // The page IS the server page now — no client slice.
  const paginated = filtered;
  type MasterRow = (typeof paginated)[number];

  /** The lifecycle and view actions of one tour (the same buttons in the table row and on a phone card). */
  const rowActions = (row: MasterRow) => (
    <ButtonGroup label={tr("actionsFor", row.tour_name)} gap="sm">
      <Button size="sm" variant="ghost" onClick={() => { if (row.tour_id) { setDetailTourId(row.tour_id); setDetailTourName(row.tour_name); } }}>
        {tr("view")}
      </Button>
      {row.tour_id && row.master_status !== "trashed" && (
        <Button size="sm" variant="secondary" aria-expanded={versionsTour?.tourId === row.tour_id}
          onClick={() => openVersions(row.tour_id!, row.tour_name)}>
          <ChevronRight size={11} /> {tr("versions")}
        </Button>
      )}
      {canLifecycle && row.tour_id && row.master_status === "active" && (
        <Button size="sm" variant="ghost" onClick={() => toggleMasterStatus(row.tour_id!, row.tour_name, "inactive")}
          disabled={toggling === row.tour_id} title={tr("setInactiveTip")} style={{ color: K.amber }}>
          {toggling === row.tour_id ? "…" : tr("setInactive")}
        </Button>
      )}
      {canLifecycle && row.tour_id && row.master_status === "inactive" && (
        <Button size="sm" variant="ghost" onClick={() => toggleMasterStatus(row.tour_id!, row.tour_name, "active")}
          disabled={toggling === row.tour_id} title={tr("setActiveTip")} style={{ color: K.success }}>
          {toggling === row.tour_id ? "…" : tr("setActive")}
        </Button>
      )}
      {canLifecycle && row.tour_id && row.master_status !== "trashed" && (
        <Button size="sm" variant="danger" onClick={() => retireTour(row.tour_id!, row.tour_name)} title={tr("retireTip")}>
          {tr("retire")}
        </Button>
      )}
      {canLifecycle && row.tour_id && row.master_status !== "trashed" && (
        <Button size="sm" variant="danger" onClick={() => trashMaster(row.tour_id!, row.tour_name)}
          disabled={trashing === row.tour_id} title={tr("trashTip")}>
          <Trash2 size={11} />{trashing === row.tour_id ? "…" : tr("trash")}
        </Button>
      )}
      {canLifecycle && row.tour_id && row.master_status === "trashed" && (
        <Button size="sm" variant="secondary" onClick={() => restoreMaster(row.tour_id!, row.tour_name)}
          disabled={restoring === row.tour_id} title={tr("restoreTip")}>
          <RotateCcw size={11} />{restoring === row.tour_id ? "…" : tr("restore")}
        </Button>
      )}
    </ButtonGroup>
  );

  const pageIds = paginated.filter(x => x.tour_id).map(x => x.tour_id!);
  const masterColumns: ColumnDef<MasterRow, unknown>[] = [
    {
      id: "select", enableSorting: false, enableHiding: false, meta: { hideLabelOnCard: true },
      header: () => (
        <Checkbox
          aria-label={tr("selectPage")}
          checked={pageIds.length > 0 && pageIds.every(id => selectedIds.has(id))}
          onChange={e => {
            if (e.target.checked) setSelectedIds(new Set([...selectedIds, ...pageIds]));
            else setSelectedIds(new Set([...selectedIds].filter(id => !pageIds.includes(id))));
          }}
        />
      ),
      cell: ({ row: { original: row } }) => row.tour_id
        ? <Checkbox aria-label={row.tour_name} checked={selectedIds.has(row.tour_id)} onChange={() => toggleSelect(row.tour_id!)} />
        : null,
    },
    { id: "n", header: "#", enableSorting: false,
      cell: ({ row }) => <span style={{ color: K.muted2 }}>{(page - 1) * pageSize + row.index + 1}</span> },
    { id: "tour_name", header: tr("colTourName"),
      cell: ({ row: { original: row } }) => (
        <span style={{ fontWeight: 600, color: K.ink, fontFamily: serif, opacity: row.master_status === "trashed" ? 0.7 : 1 }}>{row.tour_name}</span>
      ) },
    { id: "country", header: tr("colCountry"), cell: ({ row: { original: row } }) => row.country || "—" },
    { id: "quality_score", header: tr("colScore"), cell: ({ row: { original: row } }) => (
      <span style={{ fontFamily: mono, fontWeight: 700, fontSize: 15, color: scoreColor(row.quality_score) }}>
        {row.quality_score != null ? row.quality_score.toFixed(1) : "—"}
      </span>
    ) },
    { id: "versions", header: tr("colVersions"), enableSorting: false, cell: ({ row: { original: row } }) => (
      <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
        {row.version_number != null
          ? <Badge tone="info">v{row.version_number}</Badge>
          : <span style={{ color: K.muted2, fontSize: 12 }}>—</span>}
        {/* AA-626: this tour still has failed version(s) sitting in the review queue — link over so the admin can
            dismiss the stale ones. */}
        {(row.pending_review_count ?? 0) > 0 && row.tour_id && (
          <a
            href={`/admin/review?tour_id=${row.tour_id}`}
            onClick={e => e.stopPropagation()}
            title={tr("failedPendingClick", row.pending_review_count ?? 0)}
            style={{
              fontSize: 10, fontWeight: 600, padding: "2px 7px", borderRadius: 20,
              background: K.amberSoft, color: KX.amberDeep, border: `1px solid ${KX.amberBorder}`,
              textDecoration: "none", whiteSpace: "nowrap",
            }}
          >
            {tr("failedBadge", row.pending_review_count ?? 0)}
          </a>
        )}
      </div>
    ) },
    { id: "status", header: tr("colStatus"), enableSorting: false, cell: ({ row: { original: row } }) => (
      <span style={{ display: "inline-flex", gap: 4, flexWrap: "wrap" }}>
        {statusBadge(row.status, tr)}
        {masterStatusBadge(row.master_status, tr)}
      </span>
    ) },
    { id: "actions", header: tr("colActions"), enableSorting: false, enableHiding: false, meta: { hideLabelOnCard: true },
      cell: ({ row: { original: row } }) => rowActions(row) },
  ];

  // AA-739: the full country list comes from the server facets; the page's own countries are only a
  // fallback for an older API.
  const uniqueCountries = (data?.facets?.countries?.length
    ? data.facets.countries
    : Array.from(new Set(tours.map(t => t.country).filter(Boolean))).sort()) as string[];

  const anyFilterActive = Boolean(search.trim() || countryFilter || scoreFilter || versionFilter || masterStatusFilter);

  function handleSearch(v: string) { setSearch(v); setPage(1); }

  async function exportTours(format: "csv" | "xlsx") {
    setExporting(true);
    try {
      // AA-220 (D): pass selected tour_ids when any rows are checked (mirror exportAuditCsv).
      const params = new URLSearchParams({ format });
      if (selectedIds.size > 0) {
        params.set("tour_ids", [...selectedIds].join(","));
      }
      const r = await fetch(`/api/admin/tours/export?${params.toString()}`);
      if (!r.ok) return;
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = `master_content_export.${format}`; a.click();
      URL.revokeObjectURL(url);
    } finally { setExporting(false); }
  }

  async function exportAuditCsv() {
    setExporting(true);
    try {
      const params = new URLSearchParams();
      if (selectedIds.size > 0) {
        params.set("tour_ids", [...selectedIds].join(","));
      }
      const r = await fetch(`/api/admin/export-audit?${params.toString()}`);
      if (!r.ok) return;
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = "aa_tours_audit_export.csv"; a.click();
      URL.revokeObjectURL(url);
    } finally { setExporting(false); }
  }

  const countryOptions = uniqueCountries.map(c => ({ label: c, value: c }));
  const scoreOptions = [
    { label: "9.5+", value: "9.5+" },
    { label: "9.0+", value: "9.0+" },
    { label: "8.0+", value: "8.0+" },
    { label: t("lessThan8"), value: "below8" },
  ];
  const versionOptions = [
    { label: t("v1Only"), value: "v1" },
    { label: "v2+",     value: "v2+" },
    { label: "v3+",     value: "v3+" },
  ];

  const toggleExpand = useCallback(async (tourId: string) => {
    setExpandedTours(prev => {
      const next = new Set(prev);
      next.has(tourId) ? next.delete(tourId) : next.add(tourId);
      return next;
    });
    if (!tourVersions[tourId]) {
      setVersionLoading(prev => ({ ...prev, [tourId]: true }));
      try {
        const r = await fetch(`/api/admin/tours/${tourId}/versions`);
        if (r.ok) {
          const d = await r.json();
          setTourVersions(prev => ({ ...prev, [tourId]: d.versions ?? [] }));
        }
      } finally {
        setVersionLoading(prev => ({ ...prev, [tourId]: false }));
      }
    }
  }, [tourVersions]);

  function openVersions(tourId: string, tourName: string) {
    setVersionsTour({ tourId, tourName });
    if (!expandedTours.has(tourId)) toggleExpand(tourId);
  }
  function closeVersions() {
    if (versionsTour) {
      const id = versionsTour.tourId;
      setExpandedTours(prev => { const next = new Set(prev); next.delete(id); return next; });
    }
    setVersionsTour(null);
  }

  async function promoteVersion(tourId: string, tourName: string, versionNum: number) {
    if (!confirm(t("promoteConfirm", versionNum, tourName))) return;
    setPromoting(`${tourId}-${versionNum}`);
    try {
      const r = await fetch(`/api/admin/tours/${tourId}/versions/${versionNum}/promote`, { method: "POST" });
      if (r.ok) {
        setTourVersions(prev => ({
          ...prev,
          [tourId]: (prev[tourId] ?? []).map(v => ({ ...v, is_current: v.version_num === versionNum })),
        }));
        // AA-645: the promote republishes the version and queues its atomize (tour detail / atoms follow).
        showToast(t("promoted", versionNum));
      } else {
        const err = await r.json().catch(() => ({}));
        alert(staffErrorMessage(r.status, err.detail, t("promoteFailed"), rbacT("adminOnly")));
      }
    } finally { setPromoting(null); }
  }

  function toggleSelect(tourId: string) {
    setSelectedIds(prev => {
      const next = new Set(prev);
      next.has(tourId) ? next.delete(tourId) : next.add(tourId);
      return next;
    });
  }

  function toggleVersionSel(tourId: string, vNum: number) {
    setCompareVersionSel(prev => {
      const cur = new Set(prev[tourId] ?? []);
      cur.has(vNum) ? cur.delete(vNum) : cur.add(vNum);
      return { ...prev, [tourId]: cur };
    });
  }

  function showToast(msg: string) {
    setToast(msg);
    setTimeout(() => setToast(""), 3000);
  }

  async function toggleMasterStatus(tourId: string, tourName: string, newStatus: "active" | "inactive") {
    setToggling(tourId);
    try {
      const endpoint = newStatus === "active" ? "activate" : "deactivate";
      const statusName = (newStatus === "active" ? t("tabActive") : t("tabInactive")).toLowerCase();
      const r = await fetch(`/api/admin/master/${tourId}/${endpoint}`, { method: "PATCH" });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        alert(staffErrorMessage(r.status, err.detail, t("setStatusFailed", statusName), rbacT("adminOnly")));
        return;
      }
      refetch();
      showToast(t("statusSet", tourName, statusName));
    } finally {
      setToggling(null);
    }
  }

  function trashMaster(tourId: string, tourName: string) {
    // AA-986 L2 — the confirm dialog shows the dry run (tenants warned, posts, jobs) before the trash.
    setTrashing(null);
    setRetireTarget({ id: tourId, name: tourName, action: "trash_master" });
  }

  function retireTour(tourId: string, tourName: string) {
    // AA-986 L2 — retire at the source (S0/A0): the tour stops being served everywhere; restore brings it back.
    setRetireTarget({ id: tourId, name: tourName, action: "retire_source" });
  }

  async function restoreMaster(tourId: string, tourName: string) {
    setRestoring(tourId);
    try {
      const r = await fetch(`/api/admin/master/${tourId}/restore`, { method: "PATCH" });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        alert(staffErrorMessage(r.status, err.detail, t("restoreFailed"), rbacT("adminOnly")));
        return;
      }
      refetch();
      showToast(t("restored", tourName));
    } finally {
      setRestoring(null);
    }
  }

  if (loading) {
    return (
        <PageFrame>
          <LoadingScreen message={t("loadingPage")} />
        </PageFrame>
    );
  }

  return (
    <>
      {toast && (
        <div style={{
          position: "fixed", bottom: 24, right: 24, zIndex: 9999,
          background: KX.toastBg, color: KX.onDark, padding: "10px 20px",
          borderRadius: 8, fontSize: 13, fontWeight: 500,
          boxShadow: KX.shadowMd,
        }}>
          {toast}
        </div>
      )}

      {/* AA-718: define the spin keyframe (used by Refresh icon + kit StatusBadge running state). */}
      <style>{`@keyframes spin{to{transform:rotate(360deg)}}`}</style>

      {/* Main area: flex column, fills height */}
      <PageFrame variant="split" style={{ overflow: "hidden" }}>

        {/* ── Section 1: Page header + Stats (fixed) ──────────────────────── */}
        <PageFrameHeader>
          <PageHeader
            title={t("title")}
            description={t("tenantLine", AA_INTERNAL_ID.slice(0, 8))}
            actions={
              <Button variant="secondary" size="sm" onClick={refresh} disabled={refreshing}>
                <RefreshCw size={13} style={{ animation: refreshing ? "spin 1s linear infinite" : "none" }} />
                {refreshing ? t("refreshing") : t("refresh")}
              </Button>
            }
          />

          {error && (
            <div style={{ padding: "10px 14px", background: K.dangerSoft, color: K.danger, borderRadius: 7, fontSize: 13, marginBottom: 12 }}>
              {error}
            </div>
          )}

          {/* AA-718: stats are now filter-aware (match the table's current filter) + the cost is a
              labelled 30-day window reconciled with Cost Operations, with the all-time figure as
              context. */}
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 160px), 1fr))", gap: 12 }}>
            <KpiTile icon={<BarChart2 size={16} />}    label={t("toursFiltered")}
              value={String(summary?.catalog_total ?? serverTotal)}
              hint={anyFilterActive ? t("matchingFilters") : t("wholeCatalog")} />
            <KpiTile icon={<Star size={16} />}          label={t("avgQuality")}
              value={summary?.avg_quality != null ? summary.avg_quality.toFixed(1) : "—"}
              hint={t("avgFiltered")} valueTone={scoreTone(summary?.avg_quality ?? null)} />
            <KpiTile icon={<DollarSign size={16} />}   label={t("llmCost")}
              value={`$${(summary?.llm_cost_window_usd ?? summary?.total_llm_cost_usd ?? 0).toFixed(2)}`}
              hint={t("costSub", summary?.llm_cost_window_label ?? t("last30"), (summary?.total_llm_cost_usd ?? 0).toFixed(2))} />
            {/* AA-722: Score distribution of the master pool (was the stale "Pipeline Runs" card,
                fed by shared.pipeline_runs). Compact bar list — same data the retired Dashboard's
                Content Library tab showed (GET /admin/metrics/library · score_distribution). */}
            <Section>
              <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 10 }}>
                <div style={{ padding: 8, borderRadius: 8, background: alpha(K.accent, 8), color: K.accent }}>
                  <BarChart2 size={16} />
                </div>
                <span style={{ fontSize: 12, color: K.muted }}>{t("scoreDistribution")}</span>
              </div>
              {scoreDistribution.length === 0 ? (
                <div style={{ fontSize: 12, color: K.muted2 }}>{t("noDistribution")}</div>
              ) : (
                <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
                  {scoreDistribution.map(b => (
                    <div key={b.range} style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 11 }}>
                      <span style={{ width: 34, color: K.muted2, fontFamily: mono, flexShrink: 0 }}>{b.range}</span>
                      <div style={{ flex: 1, minWidth: 0, height: 8, borderRadius: 4, background: K.line2 }}>
                        <div style={{
                          width: `${scoreDistMax > 0 ? Math.round((b.count / scoreDistMax) * 100) : 0}%`,
                          height: "100%", borderRadius: 4, background: K.accent,
                        }} />
                      </div>
                      <span style={{ width: 32, textAlign: "right", color: K.ink, fontWeight: 600, fontVariantNumeric: "tabular-nums" as const, flexShrink: 0 }}>{b.count}</span>
                    </div>
                  ))}
                </div>
              )}
            </Section>
          </div>
        </PageFrameHeader>

        {/* ── Section 2: Rewritten Tours (flex, scrollable) ───────────────── */}
        <div style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden", borderBottom: `2px solid ${K.line}` }}>
          {/* Master status tabs */}
          <div style={{ borderBottom: `1px solid ${K.line}`, background: K.card, flexShrink: 0, padding: isPhone ? "0 16px" : "0 36px" }}>
            <Tabs
              variant="underline"
              ariaLabel={t("rewrittenTours")}
              style={{ marginBottom: -1 }}
              active={masterStatusFilter}
              onChange={(k) => { setMasterStatusFilter(k); setPage(1); }}
              tabs={[
                { key: "active", label: t("tabActive") },
                { key: "inactive", label: t("tabInactive") },
                { key: "trashed", label: t("tabTrashed"),
                  count: tours.filter(x => x.master_status === "trashed").length || undefined },
                { key: "", label: t("tabAll") },
              ]}
            />
          </div>

          {/* Sticky inner header */}
          <div style={{
            position: "sticky", top: 0, zIndex: 5, background: K.card,
            padding: isPhone ? "10px 16px 8px" : "10px 36px 8px", borderBottom: `1px solid ${K.line}`,
            display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, flexShrink: 0,
          }}>
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <Eyebrow style={{ margin: 0 }}>{t("rewrittenTours")}</Eyebrow>
              <span style={{ fontSize: 12, color: K.muted2 }}>
                {/* AA-718: true server total, not the length of a capped page. */}
                {t("toursCount", serverTotal, anyFilterActive)}
              </span>
            </div>

            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              {/* Inline filters */}
              <Input
                value={search}
                onChange={e => handleSearch(e.target.value)}
                placeholder={t("searchPlaceholder")}
                aria-label={t("searchPlaceholder")}
                style={{ padding: "5px 10px", border: `1px solid ${K.line}`, borderRadius: 6, fontSize: 12, fontFamily: sans, width: 180, background: K.card, color: K.ink, outline: "none" }}
              />
              <Select value={countryFilter} onChange={e => { setCountryFilter(e.target.value); setPage(1); }}
                style={{ padding: "5px 8px", border: `1px solid ${K.line}`, borderRadius: 6, fontSize: 12, fontFamily: sans, background: K.card, color: K.ink }}>
                <option value="">{t("allCountries")}</option>
                {countryOptions.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
              </Select>
              <Select value={scoreFilter} onChange={e => { setScoreFilter(e.target.value); setPage(1); }}
                style={{ padding: "5px 8px", border: `1px solid ${K.line}`, borderRadius: 6, fontSize: 12, fontFamily: sans, background: K.card, color: K.ink }}>
                <option value="">{t("allScores")}</option>
                {scoreOptions.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
              </Select>
              <Select value={versionFilter} onChange={e => { setVersionFilter(e.target.value); setPage(1); }}
                style={{ padding: "5px 8px", border: `1px solid ${K.line}`, borderRadius: 6, fontSize: 12, fontFamily: sans, background: K.card, color: K.ink }}>
                <option value="">{t("allVersions")}</option>
                {versionOptions.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
              </Select>

              {/* Compare + export */}
              {selectedIds.size >= 2 && selectedIds.size <= 4 && (
                <Button variant="secondary" size="sm" onClick={() => setCompareOpen(true)}>
                  {t("compareN", selectedIds.size)}
                </Button>
              )}
              {selectedIds.size > 0 && (
                <LinkButton tone="muted" weight="regular" onClick={() => setSelectedIds(new Set())} style={{ padding: "4px 8px" }}>
                  {t("clearN", selectedIds.size)}
                </LinkButton>
              )}
              <Button variant="secondary" size="sm" disabled={exporting} onClick={exportAuditCsv}>
                <Download size={12} /> {exporting ? "…" : t("exportAudit")}
              </Button>
              <div style={{ display: "inline-flex" }}>
                <Button variant="secondary" size="sm" disabled={exporting} onClick={() => exportTours("csv")}
                  style={{ borderRadius: "6px 0 0 6px", borderRight: "none" }}>
                  <Download size={12} /> {exporting ? "…" : "CSV"}
                </Button>
                <Button variant="secondary" size="sm" disabled={exporting} onClick={() => exportTours("xlsx")}
                  style={{ borderRadius: "0 6px 6px 0" }}>
                  XLSX
                </Button>
              </div>
            </div>
          </div>

          {/* Scrollable table area */}
          <div style={{ flex: 1, minHeight: 0, overflowY: "auto" }}>
            {filtered.length === 0 ? (
              <div style={{ padding: 40, textAlign: "center" as const, color: K.muted, fontSize: 13 }}>
                {search || countryFilter || scoreFilter || versionFilter
                  ? t("noMatch")
                  : t("noTours")}
              </div>
            ) : (
              // AA-1006: a kit DataTable (a card per tour on a phone, was a hand-made card list); sorting and paging stay on
              // the server (the page's sortKey / sortDir and its own pager below). Versions open in a kit Drawer.
              <DataTable<MasterRow>
                tableId="master-content-tours"
                data={paginated}
                getRowId={(row) => row.version_id}
                searchable={false}
                paginate={false}
                sorting={[{ id: sortKey, desc: sortDir === "desc" }]}
                onSortingChange={(next) => toggleSort((next[0]?.id ?? sortKey) as typeof sortKey)}
                manualSorting
                columns={masterColumns}
              />
            )}
            {serverTotal > 0 && (
              <div style={{ padding: "12px 20px", borderTop: `1px solid ${K.line}`, display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
                {/* AA-739: choose rows per page; AA-718: paginate against the real server total. */}
                <label style={{ fontSize: 12, color: K.muted, display: "inline-flex", alignItems: "center", gap: 8 }}>
                  {t("range", (page - 1) * pageSize + 1, Math.min(page * pageSize, serverTotal), serverTotal)}
                  <Select
                    value={pageSize}
                    onChange={e => { setPageSize(Number(e.target.value)); setPage(1); }}
                    style={{ padding: "4px 8px", border: `1px solid ${K.line}`, borderRadius: 6, fontSize: 12, fontFamily: sans, background: K.card, color: K.ink }}
                  >
                    {[20, 50, 100, 200].map(n => <option key={n} value={n}>{t("perPage", n)}</option>)}
                  </Select>
                </label>
                {serverTotal > pageSize && (
                  <Pagination page={page} total={serverTotal} pageSize={pageSize} onPage={setPage} />
                )}
              </div>
            )}
          </div>
        </div>

        {/* AA-722: the "Recent Pipeline Runs" section (fed by the stale shared.pipeline_runs, which
            only S0 upload writes) was removed. The master-pool score distribution now shows in the
            stat-card row above (Score distribution). */}
      </PageFrame>

      {/* Detail panel v2 */}
      {detailTourId && (
        <TourDetailPanelV2
          tourId={detailTourId}
          tourName={detailTourName}
          onClose={() => setDetailTourId(null)}
        />
      )}

      {/* AA-1006: a tour's versions (scores, brand audit, DOCX, promote, pick two to compare) in a kit Drawer. */}
      <Drawer open={versionsTour != null} onClose={closeVersions} width={1040}
        title={versionsTour ? `${versionsTour.tourName} · ${t("versions")}` : undefined}>
        {versionsTour && (() => {
          const tourId = versionsTour.tourId;
          const tourName = versionsTour.tourName;
          const versions = tourVersions[tourId] ?? [];
          const vLoading = versionLoading[tourId] ?? false;
          const vSel = compareVersionSel[tourId] ?? new Set<number>();
          const score = (v: number | null | undefined) => (
            <span style={{ color: v != null ? scoreColor(v) : K.muted2 }}>{v != null ? v.toFixed(1) : "—"}</span>
          );
          return (
            <div>
              {/* AA-687 — the background jobs for this tour (atomize, tenant rewrites). */}
              <div style={{ padding: "0 0 10px", fontSize: 12 }}>
                <a href={`/admin/jobs?tour_id=${tourId}`} style={{ color: K.accent }}>{t("jobsLink")}</a>
              </div>
              {vLoading ? (
                <div style={{ padding: "12px 0", fontSize: 12, color: K.muted }}>{t("loadingVersions")}</div>
              ) : versions.length === 0 ? (
                <div style={{ padding: "12px 0", fontSize: 12, color: K.muted }}>{t("noVersions")}</div>
              ) : (
                <>
                  <DataTable<TourVersion>
                    tableId="master-content-versions"
                    data={versions}
                    getRowId={(v) => v.id}
                    searchable={false}
                    paginate={false}
                    compact
                    columns={[
                      { id: "pick", header: "", enableSorting: false, enableHiding: false, meta: { hideLabelOnCard: true },
                        cell: ({ row: { original: v } }) => (
                          <Checkbox
                            aria-label={`v${v.version_num}`}
                            checked={vSel.has(v.version_num)}
                            onChange={() => toggleVersionSel(tourId, v.version_num)}
                            disabled={!vSel.has(v.version_num) && vSel.size >= 2}
                          />
                        ) },
                      { id: "version", header: t("vVersion"), accessorKey: "version_num", cell: ({ row: { original: v } }) => (
                        <span>
                          <span style={{ padding: "2px 7px", borderRadius: 10, background: v.is_current ? K.accent : K.accentTint, color: v.is_current ? KX.onSolid : K.accent, fontSize: 11, fontWeight: 600 }}>
                            v{v.version_num}
                          </span>
                          {v.is_current && <span style={{ marginLeft: 6, fontSize: 10, color: K.accent }}>{t("current")}</span>}
                        </span>
                      ) },
                      { id: "model", header: t("vModel"), enableSorting: false,
                        cell: ({ row: { original: v } }) => <span style={{ fontFamily: mono, fontSize: 11, color: K.muted2 }}>{modelLabel(v.model_id)}</span> },
                      { id: "overall", header: t("vOverall"), accessorFn: (v) => v.quality_score ?? -1,
                        cell: ({ row: { original: v } }) => <span style={{ fontWeight: 700, color: scoreColor(v.quality_score) }}>{v.quality_score != null ? v.quality_score.toFixed(1) : "—"}</span> },
                      // AA-220 (H1): validate sub-scores + judge from the list endpoint.
                      { id: "brand", header: t("vBrand"), enableSorting: false, cell: ({ row: { original: v } }) => score(v.score_brand) },
                      { id: "seo", header: t("vSeo"), enableSorting: false, cell: ({ row: { original: v } }) => score(v.score_seo) },
                      { id: "struct", header: t("vStruct"), enableSorting: false, cell: ({ row: { original: v } }) => score(v.score_structure) },
                      { id: "quality", header: t("vQuality"), enableSorting: false, cell: ({ row: { original: v } }) => score(v.score_quality) },
                      { id: "judge", header: t("vJudge"), enableSorting: false, cell: ({ row: { original: v } }) => score(v.judge_score) },
                      { id: "audit", header: t("vAudit"), enableSorting: false, cell: ({ row: { original: v } }) => (
                        <BrandAuditBadge status={v.brand_audit_status ?? null} fixPassApplied={v.fix_pass_applied ?? false} codes={v.brand_audit_codes ?? []} />
                      ) },
                      { id: "date", header: t("vDate"), accessorFn: (v) => v.created_at ?? "",
                        cell: ({ row: { original: v } }) => <span style={{ color: K.muted2 }}>{v.created_at ? formatDate(v.created_at) : "—"}</span> },
                      { id: "actions", header: "", enableSorting: false, enableHiding: false, meta: { hideLabelOnCard: true },
                        cell: ({ row: { original: v } }) => (
                          <ButtonGroup label={t("versionActions", v.version_num)} gap="sm">
                            <Button size="sm" variant="ghost" onClick={() => { setDetailTourId(tourId); setDetailTourName(tourName); }}>{t("view")}</Button>
                            {/* AA-220 (H3): per-version DOCX export (shared page-scope helper) */}
                            {v.version_num > 0 && (
                              <Button size="sm" variant="ghost" onClick={() => exportVersionDocx(tourId, Number(v.version_num))} title={t("exportDocx")}>DOCX</Button>
                            )}
                            {!v.is_current && canPromote && (
                              <Button size="sm" variant="secondary" onClick={() => promoteVersion(tourId, tourName, v.version_num)}
                                disabled={promoting === `${tourId}-${v.version_num}`}>
                                {promoting === `${tourId}-${v.version_num}` ? "…" : t("promote")}
                              </Button>
                            )}
                          </ButtonGroup>
                        ) },
                    ]}
                  />
                  {vSel.size === 2 && (
                    <div style={{ padding: "8px 0", display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
                      <span style={{ fontSize: 12, color: K.muted }}>
                        {t("selectedPair", `v${[...vSel].sort((a, b) => a - b).join(" vs v")}`)}
                      </span>
                      <Button size="sm" variant="secondary" onClick={() => {
                        const nums = [...vSel].sort((a, b) => a - b) as [number, number];
                        setCompareVersionOpen({ tourId, tourName, vNums: nums });
                      }}>
                        {t("compareVersions")}
                      </Button>
                      <LinkButton tone="muted" size="sm" weight="regular"
                        onClick={() => setCompareVersionSel(p => ({ ...p, [tourId]: new Set() }))}>
                        {t("clear")}
                      </LinkButton>
                    </div>
                  )}
                </>
              )}
            </div>
          );
        })()}
      </Drawer>

      {/* AA-986 L2 — retire / trash confirm with the dry run (Q8) */}
      {retireTarget && (
        <RetireTourDialog
          key={`${retireTarget.id}:${retireTarget.action}`}
          tourId={retireTarget.id}
          tourName={retireTarget.name}
          action={retireTarget.action}
          onClose={() => setRetireTarget(null)}
          onDone={(msg) => { setRetireTarget(null); refetch(); showToast(msg); }}
        />
      )}

      {/* Cross-tour compare modal */}
      {compareOpen && (
        <CompareModal
          tourIds={[...selectedIds]}
          onClose={() => setCompareOpen(false)}
        />
      )}

      {/* Version compare modal (full-screen) */}
      {compareVersionOpen && (
        <VersionCompareModal
          tourId={compareVersionOpen.tourId}
          tourName={compareVersionOpen.tourName}
          versionNums={compareVersionOpen.vNums}
          onClose={() => setCompareVersionOpen(null)}
        />
      )}
    </>
  );
}
