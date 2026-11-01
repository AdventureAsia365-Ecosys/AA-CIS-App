"use client";

import React, { useState, useEffect, useMemo } from "react";
import { X, ChevronRight } from "lucide-react";
import {
  K, KX, Badge, Button, ButtonGroup, DataTable, IconButton, Tabs, ToastProvider, mono, sans, serif,
  useAdminIdentity, useFormat, useT, useToast, Input, Textarea, type ColumnDef,
} from "../../_kit";
import { tourTraceHref } from "@/lib/aiGateway";
import { M } from "./tour.messages";
import { SCORE_BANDS } from "@/lib/quality-registry.generated";
import { SubjectLabelSection } from "../content-quality/PieceLabel";

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
  if (s == null) return K.muted2;
  if (s >= SCORE_BANDS.top) return K.success;
  if (s >= SCORE_BANDS.pass) return K.amber;
  return K.danger;
}

/** "just now" / "5m ago" / "3h ago", then the date — in the UI language. */
function useRelTime(): (iso: string | null) => string {
  const t = useT(M);
  const { formatDate } = useFormat();
  return (iso) => {
    if (!iso) return "—";
    const diff = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
    if (diff < 60) return t("justNow");
    if (diff < 3600) return t("minutesAgo", Math.floor(diff / 60));
    if (diff < 86400) return t("hoursAgo", Math.floor(diff / 3600));
    return formatDate(iso);
  };
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
  const t = useT(M);
  const [value, setValue] = useState(initialValue);
  const [state, setState] = useState<"idle" | "saving" | "saved" | "error">("idle");
  const [lastSaved, setLastSaved] = useState(initialValue);
  // A new initialValue (the tour reloaded) resets the box while rendering (React's "adjust state on a prop change";
  // was an effect, AA-1006).
  const [prevInitial, setPrevInitial] = useState(initialValue);
  if (initialValue !== prevInitial) {
    setPrevInitial(initialValue);
    setValue(initialValue);
    setLastSaved(initialValue);
  }

  async function handleBlur() {
    if (value === lastSaved || state === "saving") return;
    setState("saving");
    try {
      await onSave(value);
      setLastSaved(value);
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
    border: `1px solid ${state === "error" ? K.danger : K.line}`,
    background: K.card, fontSize: 13, color: K.ink, fontFamily: sans,
    boxSizing: "border-box",
    resize: rows ? "vertical" : "none",
    outline: "none",
    lineHeight: 1.5,
  };

  const statusText = state === "saving" ? t("saving") : state === "saved" ? t("saved") : state === "error" ? t("saveFailed") : "";
  const statusColor = state === "saving" ? K.amber : state === "saved" ? K.success : state === "error" ? K.danger : "transparent";

  return (
    <div style={{ marginBottom: 14 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 4 }}>
        <span style={{ fontSize: 11, fontWeight: 600, color: K.muted, textTransform: "uppercase", letterSpacing: "0.1em" }}>
          {label}
        </span>
        <span style={{ fontSize: 11, color: statusColor }}>{statusText}</span>
      </div>
      {charLimit && (
        <div style={{ fontSize: 11, textAlign: "right", marginBottom: 3, color: value.length > charLimit * 0.9 ? K.amber : K.muted2 }}>
          {value.length}/{charLimit}
        </div>
      )}
      {rows ? (
        <Textarea
          value={value}
          rows={rows}
          onChange={e => handleChange(e.target.value)}
          onBlur={handleBlur}
          placeholder={placeholder}
          style={inputStyle}
        />
      ) : (
        <Input
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
  const t = useT(M);
  const rel = useRelTime();
  // The answer for the tour it was fetched for: another tour shows the loader until its own answer arrives.
  const [res, setRes] = useState<{ tourId: string; data: SeoContextResponse | null; error: boolean } | null>(null);
  const loading = res?.tourId !== tourId;
  const data = loading ? null : res.data;
  const error = !loading && res.error;

  useEffect(() => {
    let alive = true;
    fetch(`/api/admin/tours/${tourId}/seo-context`)
      .then(r => r.json())
      .then(d => { if (alive) setRes({ tourId, data: d, error: false }); })
      .catch(() => { if (alive) setRes({ tourId, data: null, error: true }); });
    return () => { alive = false; };
  }, [tourId]);

  // Highest search volume first; a header click re-sorts in the kit table (keyword A–Z, numbers high to low).
  const sorted = useMemo(
    () => [...(data?.keyword_ideas || [])]
      .sort((a, b) => (b.search_volume ?? -1) - (a.search_volume ?? -1))
      .map((k, i) => ({ ...k, rowId: `${k.keyword}-${i}` })),
    [data],
  );
  const columns = useMemo<ColumnDef<KeywordIdea & { rowId: string }, unknown>[]>(() => [
    { id: "keyword", header: t("colKeyword"), accessorFn: (k) => (k.keyword || "").toLowerCase(),
      cell: ({ row }) => <span style={{ color: K.ink }}>{row.original.keyword}</span> },
    { id: "volume", header: t("colVolume"), accessorFn: (k) => k.search_volume ?? -1, sortDescFirst: true,
      meta: { align: "right" },
      cell: ({ row }) => <span style={{ fontFamily: mono, color: K.body }}>{fmtVolume(row.original.search_volume)}</span> },
    { id: "competition", header: t("colCompetition"), accessorFn: (k) => compNumeric(k), sortDescFirst: true,
      meta: { align: "right" },
      cell: ({ row }) => <span style={{ color: K.muted }}>{compDisplay(row.original)}</span> },
    { id: "cpc", header: t("colCpc"), accessorFn: (k) => k.cpc ?? -1, sortDescFirst: true, meta: { align: "right" },
      cell: ({ row }) => <span style={{ fontFamily: mono, color: K.accent }}>{fmtCpc(row.original.cpc)}</span> },
  ], [t]);

  if (loading) {
    return <div style={{ textAlign: "center", padding: 40, color: K.muted }}>{t("loadingSeo")}</div>;
  }
  if (error) {
    return <div style={{ textAlign: "center", padding: 40, color: K.danger }}>{t("seoFailed")}</div>;
  }
  if (!data || !data.has_data) {
    return (
      <div style={{ textAlign: "center", padding: 48, color: K.muted, fontSize: 13 }}>
        {t("noSeoData")}
      </div>
    );
  }

  return (
    <div>
      {/* Header — seed + buyer market + fetched_at */}
      <div style={{
        display: "flex", gap: 16, flexWrap: "wrap", padding: "10px 14px",
        background: K.bg, borderRadius: 8, marginBottom: 18, fontSize: 12, color: K.muted,
      }}>
        <span>{t("seed")}: <strong style={{ color: K.ink }}>{data.seed || "—"}</strong></span>
        <span>
          {t("buyerMarket")}: <strong style={{ color: K.ink }}>{data.buyer_market || "—"}</strong>
          {data.buyer_market_location_code != null && (
            <span style={{ color: K.muted2 }}> ({data.buyer_market_location_code})</span>
          )}
        </span>
        <span>{t("fetched")}: <strong style={{ color: K.ink }}>{rel(data.fetched_at)}</strong></span>
        <span>{t("ideas")}: <strong style={{ color: K.ink }}>{data.keyword_ideas.length}</strong></span>
      </div>

      {sorted.length === 0 ? (
        <div style={{ textAlign: "center", padding: 40, color: K.muted, fontSize: 13 }}>
          {t("noIdeas")}
        </div>
      ) : (
        <DataTable<KeywordIdea & { rowId: string }>
          tableId="tour-seo-keywords"
          data={sorted}
          columns={columns}
          getRowId={(k) => k.rowId}
          searchable={false}
          paginate={false}
          compact
        />
      )}
    </div>
  );
}

type PanelProps = {
  tourId: string;
  tourName: string;
  rewriteCount?: number;
  onClose: () => void;
};

/** The tour detail slide-over. A new tour remounts the panel (fresh state, its own load), and the kit toasts of the
 *  panel's saves show in its own provider. */
export function TourDetailPanelV2(props: PanelProps) {
  return (
    <ToastProvider>
      <TourDetailPanel key={props.tourId} {...props} />
    </ToastProvider>
  );
}

function TourDetailPanel({ tourId, tourName, rewriteCount = 0, onClose }: PanelProps) {
  const t = useT(M);
  const toast = useToast();
  const { isAdmin } = useAdminIdentity();
  const rel = useRelTime();
  const [detail, setDetail] = useState<TourDetailFull | null>(null);
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState<"original" | "rewrite" | "history" | "seo" | "published">("original");
  // null = not loaded: opening the History tab clears it, so the tab always shows fresh rows.
  const [historyRows, setHistoryRows] = useState<HistoryRow[] | null>(null);
  const history = historyRows ?? [];
  const loadingHistory = tab === "history" && historyRows === null;
  const [selectedVersion, setSelectedVersion] = useState<VersionContent | null>(null);
  const [loadingVersion, setLoadingVersion] = useState(false);
  const [promoting, setPromoting] = useState(false);
  const [rawDraft, setRawDraft] = useState<Record<string, string>>({});
  const [savingAll, setSavingAll] = useState(false);

  useEffect(() => {
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
    if (tab !== "history" || historyRows !== null) return;
    let alive = true;
    fetch(`/api/admin/tours/${tourId}/history`)
      .then(r => r.json())
      .then(d => { if (alive) setHistoryRows(d.history || []); })
      .catch(() => { if (alive) setHistoryRows([]); });
    return () => { alive = false; };
  }, [tab, tourId, historyRows]);

  function openTab(next: typeof tab) {
    if (next === "history") setHistoryRows(null);
    setTab(next);
  }

  function showToast(msg: string, type: "success" | "error") {
    if (type === "success") toast.success(msg, 3000);
    else toast.error(msg, 3000);
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
    if (!confirm(t("promoteConfirm", versionNum))) return;
    setPromoting(true);
    try {
      const r = await fetch(`/api/admin/tours/${tourId}/versions/${versionNum}/promote`, { method: "POST" });
      if (r.ok) {
        showToast(t("promoted", versionNum), "success");
        setHistoryRows(prev => prev && prev.map(h => ({ ...h })));
      } else {
        // AA-645: 409 = version not approved / source not live; the API says which.
        const err = await r.json().catch(() => ({}));
        showToast(typeof err.detail === "string" && err.detail ? err.detail : t("promoteFailed"), "error");
      }
    } finally { setPromoting(false); }
  }

  async function saveRaw(field: string, value: string | string[]) {
    const res = await fetch(`/api/admin/tours/${tourId}/raw`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ [field]: value }),
    });
    if (!res.ok) { showToast(t("saveFailed"), "error"); throw new Error("Save failed"); }
    showToast(t("savedToast"), "success");
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
      if (!res.ok) { showToast(t("saveFailed"), "error"); return; }
      showToast(t("allSaved"), "success");
    } finally { setSavingAll(false); }
  }

  async function saveGenerated(field: string, value: string | string[]) {
    const genId = detail?.generated?.id;
    if (!genId) { showToast(t("noGenerated"), "error"); throw new Error("No generated content"); }
    const res = await fetch(`/api/admin/tours/${tourId}/generated/${genId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ [field]: value }),
    });
    if (!res.ok) { showToast(t("saveFailed"), "error"); throw new Error("Save failed"); }
    showToast(t("savedToast"), "success");
  }

  const historyColumns: ColumnDef<HistoryRow, unknown>[] = [
    { id: "version", header: t("hVersion"), accessorFn: (h) => h.version_num, cell: ({ row: { original: h } }) => {
      const isActive = selectedVersion?.version_num === h.version_num;
      return (
        <span style={{ fontSize: 12, padding: "2px 8px", borderRadius: 10, background: isActive ? K.accent : K.accentTint, color: isActive ? KX.onAccent : K.accent, fontWeight: 600 }}>
          v{h.version_num}
        </span>
      );
    } },
    { id: "model", header: t("hModel"), accessorFn: (h) => modelShort(h.model_editorial),
      cell: ({ getValue }) => <span style={{ fontFamily: mono, fontSize: 11, color: K.muted }}>{String(getValue())}</span> },
    // AA-318: the prompt version (sha256[:8], AA-289) as a mono pill; a change between rows is the regression signal.
    { id: "prompt", header: t("hPrompt"), accessorFn: (h) => h.prompt_version ?? "", cell: ({ row: { original: h } }) => (
      h.prompt_version ? (
        <span style={{ fontFamily: mono, fontSize: 10.5, padding: "1px 6px", borderRadius: 8, background: K.line2, color: K.ink2 }} title={`prompt_version ${h.prompt_version}`}>
          {h.prompt_version}
        </span>
      ) : <span style={{ color: K.muted2 }}>—</span>
    ) },
    { id: "brandV", header: t("hBrandV"), enableSorting: false, cell: ({ row: { original: h } }) => (
      h.brand_rules_version != null
        ? <span style={{ padding: "1px 6px", borderRadius: 8, background: K.accentTint, color: K.accent, fontWeight: 600, fontSize: 10 }}>{h.brand_name ? `${h.brand_name} · v${h.brand_rules_version}` : `v${h.brand_rules_version}`}</span>
        : <span style={{ fontSize: 11, color: K.muted2 }}>—</span>
    ) },
    { id: "overall", header: t("hOverall"), accessorFn: (h) => h.score_overall ?? undefined, sortUndefined: "last", cell: ({ row: { original: h } }) => (
      <span style={{ fontWeight: 700, color: scoreColor(h.score_overall) }}>{h.score_overall != null ? h.score_overall.toFixed(1) : "—"}</span>
    ) },
    { id: "brand", header: t("hBrand"), accessorFn: (h) => h.score_brand ?? undefined, sortUndefined: "last", cell: ({ row: { original: h } }) => (
      <span style={{ color: scoreColor(h.score_brand) }}>{h.score_brand != null ? h.score_brand.toFixed(1) : "—"}</span>
    ) },
    { id: "seo", header: t("hSeo"), accessorFn: (h) => h.score_seo ?? undefined, sortUndefined: "last", cell: ({ row: { original: h } }) => (
      <span style={{ color: scoreColor(h.score_seo) }}>{h.score_seo != null ? h.score_seo.toFixed(1) : "—"}</span>
    ) },
    { id: "cost", header: t("hCost"), accessorFn: (h) => h.cost_usd ?? undefined, sortUndefined: "last", cell: ({ row: { original: h } }) => (
      <span style={{ color: K.accent }}>{h.cost_usd != null ? `$${h.cost_usd.toFixed(4)}` : "—"}</span>
    ) },
    { id: "date", header: t("hDate"), accessorFn: (h) => h.created_at ?? "", cell: ({ row: { original: h } }) => (
      <span style={{ color: K.muted2, fontSize: 12 }}>{rel(h.created_at)}</span>
    ) },
    { id: "actions", header: "", enableSorting: false, meta: { hideLabelOnCard: true }, cell: ({ row: { original: h } }) => {
      const isActive = selectedVersion?.version_num === h.version_num;
      return (
        <ButtonGroup label={t("versionActions", h.version_num)} gap="sm">
          <Button size="sm" variant="ghost" disabled={loadingVersion}
            onClick={e => { e.stopPropagation(); loadVersion(h.version_num); }}>
            {loadingVersion && isActive ? "…" : t("viewContent")}
          </Button>
          <Button size="sm" variant="secondary" disabled={promoting}
            onClick={e => { e.stopPropagation(); promoteVersion(h.version_num); }}>
            {promoting ? "…" : t("makeCurrent")}
          </Button>
        </ButtonGroup>
      );
    } },
  ];

  const raw = detail?.raw;
  const gen = detail?.generated;
  const pub = detail?.published;

  return (
    <>
      {/* Backdrop */}
      <div
        onClick={onClose}
        style={{ position: "fixed", inset: 0, background: KX.scrim, zIndex: 199 }}
      />

      {/* Panel */}
      <div style={{
        position: "fixed", top: 0, right: 0, bottom: 0,
        width: "70vw", background: K.card,
        boxShadow: KX.shadowLg,
        zIndex: 200, display: "flex", flexDirection: "column",
        fontFamily: sans,
      }}>
        {/* Header */}
        <div style={{ padding: "16px 24px 0", borderBottom: `1px solid ${K.line}`, flexShrink: 0 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 10 }}>
            <div style={{ flex: 1, paddingRight: 12 }}>
              {/* Badges */}
              <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 6 }}>
                {raw?.country && <Badge tone="info">{raw.country}</Badge>}
                {raw?.pipeline_status && (
                  <Badge tone={raw.pipeline_status === "published" ? "success" : "neutral"}>
                    {raw.pipeline_status}
                  </Badge>
                )}
                {gen?.score_overall != null && (
                  <Badge tone={gen.score_overall >= SCORE_BANDS.top ? "success" : gen.score_overall >= SCORE_BANDS.pass ? "warning" : "danger"}>
                    ★ {gen.score_overall.toFixed(1)}
                  </Badge>
                )}
              </div>
              {/* Name */}
              <div style={{ fontFamily: serif, fontSize: 17, fontWeight: 600, color: K.ink, lineHeight: 1.3, marginBottom: 5 }}>
                {gen?.aa_name || tourName}
              </div>
              {/* Meta */}
              <div style={{ fontSize: 11, color: K.muted, display: "flex", gap: 12, flexWrap: "wrap" }}>
                <span>{t("idShort", tourId.slice(0, 8))}</span>
                {/* AA-804 UI part 3: every paid call of the tour (AI providers is admin only, so is the link). */}
                {isAdmin && (
                  <a href={tourTraceHref(tourId)} data-testid="tour-open-trace" title={t("openTraceTitle")}
                    style={{ color: K.accent, textDecoration: "none" }}>{t("openTrace")}</a>
                )}
                {rewriteCount > 0 && <span>{t("rewritten", rewriteCount)}</span>}
                {pub?.published_at && <span>{t("publishedWhen", rel(pub.published_at))}</span>}
              </div>
            </div>
            <IconButton variant="plain" onClick={onClose} label={t("close")}>
              <X size={18} />
            </IconButton>
          </div>
          {/* Tabs */}
          <Tabs
            variant="underline"
            ariaLabel={t("tabsLabel")}
            gap={8}
            style={{ marginBottom: -1 }}
            active={tab}
            onChange={(k) => openTab(k as typeof tab)}
            tabs={[
              { key: "original", label: t("tabOriginal") },
              { key: "rewrite", label: t("tabRewrite"), disabled: !gen },
              { key: "history", label: t("tabHistory") },
              { key: "seo", label: t("tabSeo") },
              { key: "published", label: t("tabPublished"), disabled: !pub },
            ]}
          />
        </div>

        {/* Body */}
        <div style={{ flex: 1, minHeight: 0, overflowY: "auto", padding: "20px 24px" }}>
          {loading ? (
            <div style={{ textAlign: "center", padding: 40, color: K.muted }}>{t("loading")}</div>
          ) : !detail ? (
            <div style={{ textAlign: "center", padding: 40, color: K.danger }}>{t("detailFailed")}</div>
          ) : tab === "original" ? (
            <div>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0 24px" }}>
                <EditableField label={t("fTourName")} initialValue={raw?.src_name || ""} onSave={v => saveRaw("src_name", v)} onChange={v => setRawDraft(d => ({ ...d, src_name: v }))} />
                <EditableField label={t("fCountry")} initialValue={raw?.country || ""} onSave={v => saveRaw("country", v)} onChange={v => setRawDraft(d => ({ ...d, country: v }))} />
                <EditableField label={t("fDuration")} initialValue={raw?.duration || ""} onSave={v => saveRaw("duration", v)} onChange={v => setRawDraft(d => ({ ...d, duration: v }))} />
                <EditableField label={t("fGroupSize")} initialValue={raw?.group_size || ""} onSave={v => saveRaw("group_size", v)} onChange={v => setRawDraft(d => ({ ...d, group_size: v }))} />
                <EditableField label={t("fPrice")} initialValue={raw?.price_raw || ""} onSave={v => saveRaw("price_raw", v)} onChange={v => setRawDraft(d => ({ ...d, price_raw: v }))} />
                <EditableField label={t("fPeriod")} initialValue={raw?.period || ""} onSave={v => saveRaw("period", v)} onChange={v => setRawDraft(d => ({ ...d, period: v }))} />
                <EditableField label={t("fProvider")} initialValue={raw?.provider || ""} onSave={v => saveRaw("provider", v)} onChange={v => setRawDraft(d => ({ ...d, provider: v }))} />
              </div>
              <EditableField label={t("fSummary")} initialValue={raw?.src_summary || ""} onSave={v => saveRaw("src_summary", v)} onChange={v => setRawDraft(d => ({ ...d, src_summary: v }))} rows={6} />
              <EditableField
                label={t("fHighlightsLines")}
                initialValue={arrayToText(raw?.src_highlights)}
                onSave={v => saveRaw("src_highlights", textToArray(v))}
                onChange={v => setRawDraft(d => ({ ...d, src_highlights: v }))}
                rows={4}
                placeholder={t("fHighlightPlaceholder")}
              />
              <EditableField label={t("fItineraries")} initialValue={raw?.src_itineraries || ""} onSave={v => saveRaw("src_itineraries", v)} onChange={v => setRawDraft(d => ({ ...d, src_itineraries: v }))} rows={10} />
              <EditableField label={t("fDescription")} initialValue={raw?.src_description || ""} onSave={v => saveRaw("src_description", v)} onChange={v => setRawDraft(d => ({ ...d, src_description: v }))} rows={6} />
              <EditableField label={t("fInclusions")} initialValue={raw?.inclusions || ""} onSave={v => saveRaw("inclusions", v)} onChange={v => setRawDraft(d => ({ ...d, inclusions: v }))} rows={4} />
              <EditableField label={t("fExclusions")} initialValue={raw?.exclusions || ""} onSave={v => saveRaw("exclusions", v)} onChange={v => setRawDraft(d => ({ ...d, exclusions: v }))} rows={4} />
              <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 8 }}>
                <Button variant="primary" onClick={saveAllRaw} disabled={savingAll}>
                  {savingAll ? t("saving") : t("saveAll")}
                </Button>
              </div>
            </div>
          ) : tab === "rewrite" ? (
            gen ? (
              <div>
                {/* Info bar */}
                <div style={{
                  display: "flex", gap: 16, flexWrap: "wrap", padding: "10px 14px",
                  background: K.bg, borderRadius: 8, marginBottom: 20, fontSize: 12, color: K.muted,
                }}>
                  <span>{t("model")}: <strong>{modelShort(gen.model_editorial)}</strong></span>
                  <span>{t("score")}: <strong style={{ color: scoreColor(gen.score_overall) }}>{gen.score_overall?.toFixed(1) ?? "—"}</strong></span>
                  <span>{t("generatedWhen")}: <strong>{rel(gen.created_at)}</strong></span>
                  <span>{t("version")}: <strong>v{gen.version_num}</strong></span>
                </div>
                {/* AA-209: full sub-score breakdown (incl. Quality) + judge-capped flag + brand judge */}
                {(() => {
                  const subs = [gen.score_brand, gen.score_seo, gen.score_structure, gen.score_quality];
                  const vAvg = subs.every(s => s != null)
                    ? (subs as number[]).reduce((a, s) => a + s, 0) / 4 : null;
                  const capped = gen.score_overall != null && vAvg != null && gen.score_overall < vAvg - 0.01;
                  const fmt = (s: number | null) => (s != null ? s.toFixed(1) : "—");
                  const rows: [string, number | null][] = [
                    [t("sOverall"), gen.score_overall], [t("sBrand"), gen.score_brand], [t("sSeo"), gen.score_seo],
                    [t("sStructure"), gen.score_structure], [t("sQuality"), gen.score_quality],
                  ];
                  return (
                    <div style={{ marginBottom: 20 }}>
                      <div style={{ display: "flex", gap: 14, flexWrap: "wrap", marginBottom: capped ? 8 : 0 }}>
                        {rows.map(([label, sc]) => (
                          <span key={label} style={{ fontSize: 12, color: K.muted }}>
                            {label}: <strong style={{ color: scoreColor(sc) }}>{fmt(sc)}</strong>
                          </span>
                        ))}
                      </div>
                      {capped && <Badge tone="warning">{t("judgeCapped", fmt(vAvg))}</Badge>}
                      <div style={{ marginTop: 12, paddingTop: 12, borderTop: `1px solid ${K.line}` }}>
                        <div style={{ fontSize: 10, letterSpacing: "0.1em", textTransform: "uppercase", color: K.muted2, marginBottom: 6 }}>
                          {t("brandJudge")}
                        </div>
                        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                          {gen.judge ? (
                            <>
                              <Badge tone="accent">{t("fit", fmt(gen.judge.brand_fit))}</Badge>
                              <Badge tone="accent">{t("distinct", fmt(gen.judge.distinct))}</Badge>
                              <Badge tone={gen.judge.mission_present ? "success" : "danger"}>
                                {t("mission", !!gen.judge.mission_present)}
                              </Badge>
                            </>
                          ) : (
                            <span style={{ fontSize: 12, color: K.muted2 }}>{t("notRun")}</span>
                          )}
                          {gen.brand_audit_status && (
                            <Badge tone={gen.brand_audit_status === "pass" ? "success" : "warning"}>
                              {gen.brand_audit_status}
                            </Badge>
                          )}
                        </div>
                        {gen.judge?.feedback && (
                          <div style={{ fontSize: 12, color: K.body, lineHeight: 1.6, marginTop: 8 }}>
                            {gen.judge.feedback}
                          </div>
                        )}
                      </div>
                    </div>
                  );
                })()}
                <EditableField label={t("fAaName")} initialValue={gen.aa_name || ""} onSave={v => saveGenerated("aa_name", v)} />
                <EditableField label={t("fSubtitle")} initialValue={gen.aa_subtitle || ""} onSave={v => saveGenerated("aa_subtitle", v)} />
                <EditableField label={t("fSummary")} initialValue={gen.aa_summary || ""} onSave={v => saveGenerated("aa_summary", v)} rows={6} />
                <EditableField
                  label={t("fHighlightsLines")}
                  initialValue={arrayToText(gen.aa_highlights)}
                  onSave={v => saveGenerated("aa_highlights", textToArray(v))}
                  rows={4}
                />
                <EditableField label={t("fItineraries")} initialValue={gen.aa_itineraries || ""} onSave={v => saveGenerated("aa_itineraries", v)} rows={10} />
                <EditableField label={t("fSeoTitle")} initialValue={gen.seo_title || ""} onSave={v => saveGenerated("seo_title", v)} charLimit={70} />
                <EditableField label={t("fSeoMeta")} initialValue={gen.seo_meta || ""} onSave={v => saveGenerated("seo_meta", v)} rows={3} charLimit={170} />
                <EditableField
                  label={t("fKeywords")}
                  initialValue={arrayToComma(gen.seo_keywords_used)}
                  onSave={v => saveGenerated("seo_keywords_used", commaToArray(v))}
                  placeholder={t("keywordsPlaceholder")}
                />
                {/* AA-923 WM-32 — the human label of this rewrite (the T9 piece label criteria), admin only */}
                <div style={{ marginTop: 20, paddingTop: 12, borderTop: `1px solid ${K.line}` }}>
                  <div style={{ fontSize: 10, letterSpacing: "0.1em", textTransform: "uppercase", color: K.muted2, marginBottom: 6 }}>
                    {t("labelSection")}
                  </div>
                  <SubjectLabelSection subjectType="a1_content" subjectId={gen.id} />
                </div>
              </div>
            ) : (
              <div style={{ textAlign: "center", padding: 40, color: K.muted }}>{t("noRewrite")}</div>
            )
          ) : tab === "history" ? (
            loadingHistory ? (
              <div style={{ textAlign: "center", padding: 40, color: K.muted }}>{t("loadingHistory")}</div>
            ) : history.length === 0 ? (
              <div style={{ textAlign: "center", padding: 40, color: K.muted, fontSize: 13 }}>{t("noRewrites")}</div>
            ) : (
              <div>
                <DataTable<HistoryRow>
                  tableId="tour-rewrite-history"
                  data={history}
                  columns={historyColumns}
                  getRowId={(h) => h.id}
                  onRowClick={(h) => loadVersion(h.version_num)}
                  searchable={false}
                  paginate={false}
                  compact
                />

                {/* Version content preview */}
                {loadingVersion && (
                  <div style={{ marginTop: 16, padding: 20, textAlign: "center", color: K.muted, fontSize: 13 }}>{t("loadingVersion")}</div>
                )}
                {selectedVersion && !loadingVersion && (
                  <div style={{ marginTop: 20, padding: "16px 20px", background: K.bg, borderRadius: 8, border: `1px solid ${K.line}` }}>
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 14 }}>
                      <div style={{ fontSize: 12, fontWeight: 600, color: K.muted, textTransform: "uppercase", letterSpacing: "0.08em" }}>
                        v{selectedVersion.version_num} — {modelShort(selectedVersion.model_id)}
                        {selectedVersion.quality_score != null && (
                          <span style={{ marginLeft: 10, color: scoreColor(selectedVersion.quality_score) }}>
                            ★ {selectedVersion.quality_score.toFixed(1)}
                          </span>
                        )}
                      </div>
                      <Button size="sm" variant="primary" onClick={() => promoteVersion(selectedVersion.version_num)} disabled={promoting}>
                        {promoting ? t("promoting") : t("makeCurrent")}
                      </Button>
                    </div>
                    {[
                      { label: t("vName"),        val: selectedVersion.aa_name },
                      { label: t("fSubtitle"),    val: selectedVersion.aa_subtitle },
                      { label: t("fSummary"),     val: selectedVersion.aa_summary },
                      { label: t("fItineraries"), val: selectedVersion.aa_itineraries },
                      { label: t("fSeoTitle"),   val: selectedVersion.seo_title },
                      { label: t("fSeoMeta"),    val: selectedVersion.seo_meta },
                    ].map(({ label, val }) => val ? (
                      <div key={label} style={{ marginBottom: 12 }}>
                        <div style={{ fontSize: 10, fontWeight: 600, color: K.muted, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 4 }}>{label}</div>
                        <div style={{ fontSize: 13, color: K.body, lineHeight: 1.6, whiteSpace: "pre-wrap" }}>{val}</div>
                      </div>
                    ) : null)}
                    {selectedVersion.aa_highlights.length > 0 && (
                      <div style={{ marginBottom: 12 }}>
                        <div style={{ fontSize: 10, fontWeight: 600, color: K.muted, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 4 }}>{t("highlights")}</div>
                        <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13, color: K.body }}>
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
                  <div style={{ fontSize: 11, color: K.muted, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.1em", marginBottom: 6 }}>{t("publishedName")}</div>
                  <div style={{ fontFamily: serif, fontSize: 18, fontWeight: 500, color: K.ink }}>{pub.aa_name}</div>
                </div>
                {pub.aa_subtitle && (
                  <div>
                    <div style={{ fontSize: 11, color: K.muted, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.1em", marginBottom: 4 }}>{t("fSubtitle")}</div>
                    <div style={{ fontSize: 14, fontStyle: "italic", color: K.body }}>{pub.aa_subtitle}</div>
                  </div>
                )}
                <div>
                  <div style={{ fontSize: 11, color: K.muted, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.1em", marginBottom: 4 }}>{t("qualityScore")}</div>
                  <div style={{ fontFamily: mono, fontSize: 26, fontWeight: 700, color: scoreColor(pub.quality_score) }}>
                    {pub.quality_score?.toFixed(1) ?? "—"}
                  </div>
                </div>
                <div>
                  <div style={{ fontSize: 11, color: K.muted, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.1em", marginBottom: 4 }}>{t("publishedAt")}</div>
                  <div style={{ fontSize: 13, color: K.body }}>{rel(pub.published_at)}</div>
                </div>
                <div style={{ borderTop: `1px solid ${K.line}`, paddingTop: 16 }}>
                  <a
                    href={`/admin/s1-rewrite?tour_id=${tourId}`}
                    style={{
                      display: "inline-flex", alignItems: "center", gap: 6,
                      padding: "9px 18px", borderRadius: 8,
                      background: K.accent, border: `1px solid ${K.accent}`,
                      fontSize: 13, fontWeight: 600, color: KX.onAccent, textDecoration: "none",
                    }}
                  >
                    {t("rerun")} <ChevronRight size={13} />
                  </a>
                </div>
              </div>
            ) : (
              <div style={{ textAlign: "center", padding: 40, color: K.muted }}>{t("notPublished")}</div>
            )
          )}
        </div>
      </div>

    </>
  );
}
