"use client";

import React, { useState, useRef, useEffect } from "react";
import {
  Upload, CheckCircle, XCircle, AlertCircle, ArrowRight, Loader2,
  ChevronDown, ChevronUp, FileText, Copy, RefreshCw, Search,
  Trash2, RotateCcw,
} from "lucide-react";
import { Pagination } from "../_components/Pagination";
import {
  Button, PageFrame, PageHeader, useFormat, useT, Input, Select, K, serif, sans, mono, Section, Eyebrow, Badge,
  DataTable, IconButton, KX, LinkButton, useIsPhone, type ColumnDef,
} from "../../_kit";
import { phoneCardColumns } from "@/lib/tableSort";
import { UP } from "./messages";

const TENANT_ID = "00000000-0000-0000-0000-000000000001";

// ─── Helpers ──────────────────────────────────────────────────────────────────

function stripUuidPrefix(filename: string): string {
  return filename.replace(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}_/i, "");
}

/** "5 minutes ago" … a date, in the UI language (AA-994). */
function useRelativeTime(): (isoStr: string | null | undefined) => string {
  const t = useT(UP);
  const { formatDate } = useFormat();
  return (isoStr) => {
    if (!isoStr) return "—";
    const diff = Math.floor((Date.now() - new Date(isoStr).getTime()) / 1000);
    if (diff < 60) return t("justNow");
    if (diff < 3600) return t("minutesAgo", Math.floor(diff / 60));
    if (diff < 86400) return t("hoursAgo", Math.floor(diff / 3600));
    return formatDate(isoStr);
  };
}

// ─── Types ────────────────────────────────────────────────────────────────────

interface TourPreview {
  tour_id: string;
  src_name: string;
  jev_notes?: string[];   // AA-690 — Jev ingest observations (shadow/grey), never a block
  country: string;
  duration: string | null;
  price_raw: string | null;
  group_size: string | null;
  period: string | null;
  pipeline_status: string;
  ingest_at: string;
  src_subtitle: string | null;
  src_summary: string | null;
  src_highlights: string | null;
  src_itineraries: string | null;
  provider: string | null;
  activities: string | null;
  inclusions: string | null;
  exclusions: string | null;
  sku: string | null;
  src_description: string | null;
  links: string | null;
  feature: string | null;
  best_time_to_go: string | null;
}

interface BlockedTour {
  src_name: string;
  country: string | null;
  // AA-490, AA-604; AA-690 adds the Jev gates (only when their questions are enforced)
  reason: "duplicate_tour" | "missing_fields" | "duplicate_in_file" | "empty_itinerary" | "not_a_tour" | "thin_itinerary"
    | "duplicate_of_existing";
  missing_fields?: string[];
  message: string;
}

interface DryRunResponse {
  status: "parsed" | "blocked";
  reason?: string;
  dry_run: boolean;
  batch_id?: string | null;
  ready_count?: number;
  blocked_count?: number;
  tours?: TourPreview[];
  blocked_tours?: BlockedTour[];
  message?: string;
}

interface CommitResponse {
  status: string;
  batch_id?: string | null;
  tour_count?: number;
  tours_written?: number;
  tours_staged?: number;
  staged_ids?: string[];
}

interface StagingItem {
  staging_id: string;
  matched_tour_id: string | null;
  decision: string;
  // AA-986 L2 (Q6): the matched tour was deleted at S0/A0 (no live version left).
  matched_deleted?: boolean;
  matched_deleted_at?: string | null;
  incoming: Record<string, string | null>;
  existing: {
    src_name: string | null;
    src_summary: string | null;
    country: string | null;
    price_raw: string | null;
    provider: string | null;
    ingest_at: string | null;
    source_status?: string | null;
    deleted_at?: string | null;
  };
}

interface FileState {
  id: string;
  file: File;
  status: "pending" | "uploading" | "parsing" | "parsed" | "blocked-file" | "committing" | "done" | "error";
  s3Key?: string;
  parseResult?: DryRunResponse;
  parseError?: string;
  commitResult?: CommitResponse;
  commitError?: string;
  expanded: boolean;
}

interface UploadHistoryItem {
  id: string;
  filename: string;
  file_size_kb: number | null;
  row_count: number | null;
  // AA-344: from shared.pipeline_runs.ingest_details (migration 091, AA-343 Part C) via a
  // LEFT JOIN on batch_id — null for any source ingested before migration 091 (degrades
  // gracefully, no backfill). row_count above is the PARSED count only; rows_landed is what
  // actually made it into raw_tours (rows_dropped = parsed - landed).
  rows_landed: number | null;
  rows_dropped: number | null;
  parsed_at: string | null;
  parse_error_count: number;
  batch_id: string | null;
}

interface TourReadyItem {
  tour_id: string;
  src_name: string;
  country: string | null;
  ingest_at: string | null;
  source_id: string | null;
  batch_id: string | null;
  filename: string | null;
  source_status?: string;
  deleted_at?: string | null;
}

// ─── StepIndicator ────────────────────────────────────────────────────────────

function StepIndicator({ step }: { step: 1 | 2 | 3 | 4 | 5 }) {
  const t = useT(UP);
  const STEPS = [
    { n: 1 as const, label: t("stepSelect") },
    { n: 2 as const, label: t("stepUpload") },
    { n: 3 as const, label: t("stepParse") },
    { n: 4 as const, label: t("stepCommit") },
    { n: 5 as const, label: t("stepDup") },
  ];
  return (
    <div style={{ display: "flex", alignItems: "center", marginBottom: 28 }}>
      {STEPS.map((s, i) => {
        const done   = s.n < step;
        const active = s.n === step;
        return (
          <React.Fragment key={s.n}>
            <div style={{
              display: "flex", alignItems: "center", gap: 8,
              padding: "7px 14px", borderRadius: 20,
              background: active ? K.accent : done ? K.successSoft : K.line2,
            }}>
              <div style={{
                width: 20, height: 20, borderRadius: "50%", flexShrink: 0,
                display: "flex", alignItems: "center", justifyContent: "center",
                background: active ? KX.onSolid : done ? K.success : K.muted2,
                color: active ? K.accent : KX.onSolid, fontSize: 11, fontWeight: 700,
              }}>
                {done ? "✓" : s.n}
              </div>
              <span style={{ fontSize: 12, fontWeight: 600, whiteSpace: "nowrap",
                color: active ? KX.onSolid : done ? K.success : K.muted }}>
                {s.label}
              </span>
            </div>
            {i < STEPS.length - 1 && (
              <div style={{ width: 20, height: 1, background: K.line }} />
            )}
          </React.Fragment>
        );
      })}
    </div>
  );
}

// ─── TourRow (expandable) ─────────────────────────────────────────────────────

const DL_LABEL: React.CSSProperties = {
  fontSize: 10, fontWeight: 700, textTransform: "uppercase",
  letterSpacing: "0.12em", color: K.muted, marginBottom: 2,
};
const DL_VAL: React.CSSProperties = {
  fontSize: 12, color: K.body, lineHeight: 1.6, marginBottom: 10,
};

function DetailField({ label, value }: { label: string; value: string | null | undefined }) {
  if (!value) return null;
  return (
    <div>
      <div style={DL_LABEL}>{label}</div>
      <div style={DL_VAL}>{value}</div>
    </div>
  );
}

/** AA-1006 follow-up (S263 F2): a kit DataTable inside a flush card (`Section padded={false}`) sits at the card's
 * 20 px inset, the same as the header and filter rows: flush, its toolbar's Columns button touched the card border. */
function TableInset({ children }: { children: React.ReactNode }) {
  return <div data-testid="upload-table-inset" style={{ padding: "10px 20px 16px", minWidth: 0 }}>{children}</div>;
}

// AA-1006: the parsed tours are a kit DataTable (`ReadyToursTable`); a click on a row shows that tour's source fields
// under the table (was an extra table row under the clicked one).
function ReadyToursTable({ tours, expanded, onToggle }: {
  tours: TourPreview[]; expanded: string | null; onToggle: (tourId: string) => void;
}) {
  const t = useT(UP);
  const open = tours.find(x => x.tour_id === expanded) ?? null;
  const rows = tours.map((tour, i) => ({ tour, idx: i + 1 }));
  const text = (v: string | null | undefined) => v || "—";
  const isPhone = useIsPhone();
  return (
    <>
      <DataTable<{ tour: TourPreview; idx: number }>
        tableId="upload-parsed-ready"
        data={rows}
        getRowId={(r) => r.tour.tour_id}
        onRowClick={(r) => onToggle(r.tour.tour_id)}
        searchable={false}
        paginate={false}
        compact
        // On a phone the tour name is the card title (the row number stays first on a desktop).
        columns={phoneCardColumns<ColumnDef<{ tour: TourPreview; idx: number }, unknown>>([
          { id: "idx", header: "#", enableSorting: false, cell: ({ row }) => <span style={{ color: K.muted2 }}>{row.original.idx}</span> },
          { id: "name", header: t("colTourName"), enableSorting: false, cell: ({ row: { original: { tour } } }) => (
            <span style={{ fontWeight: 600, color: K.ink }}>
              {tour.src_name || "—"}
              {(tour.jev_notes ?? []).map(n => (
                <span key={n} style={{ display: "block", fontSize: 11, fontWeight: 400, color: K.amber, marginTop: 2 }}>{n}</span>
              ))}
            </span>
          ) },
          { id: "country", header: t("colCountry"), enableSorting: false, cell: ({ row }) => text(row.original.tour.country) },
          { id: "duration", header: t("colDuration"), enableSorting: false, cell: ({ row }) => text(row.original.tour.duration) },
          { id: "price", header: t("colPrice"), enableSorting: false, cell: ({ row }) => text(row.original.tour.price_raw) },
          { id: "group", header: t("colGroupSize"), enableSorting: false, cell: ({ row }) => text(row.original.tour.group_size) },
          { id: "period", header: t("colPeriod"), enableSorting: false, cell: ({ row }) => text(row.original.tour.period) },
          { id: "provider", header: t("colProvider"), enableSorting: false, cell: ({ row }) => text(row.original.tour.provider) },
          { id: "sku", header: t("colSku"), enableSorting: false, cell: ({ row }) => text(row.original.tour.sku) },
          { id: "status", header: t("colStatus"), enableSorting: false, cell: ({ row }) => (
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <Badge tone="success">{t("ready")}</Badge>
              {expanded === row.original.tour.tour_id
                ? <ChevronUp size={13} style={{ color: K.muted2 }} />
                : <ChevronDown size={13} style={{ color: K.muted2 }} />}
            </div>
          ) },
        ], "name", isPhone)}
      />
      {open && <TourDetail tour={open} />}
    </>
  );
}

function TourDetail({ tour }: { tour: TourPreview }) {
  const t = useT(UP);
  const itinPreview = tour.src_itineraries
    ? (tour.src_itineraries.length > 200
        ? tour.src_itineraries.slice(0, 200) + "..."
        : tour.src_itineraries)
    : null;
  return (
    <div style={{ padding: "16px 20px 18px", background: K.bg, borderTop: `1px solid ${K.line}`, borderBottom: `1px solid ${K.line}` }}>
      <div style={{ fontSize: 13, fontWeight: 600, color: K.ink, marginBottom: 10 }}>{tour.src_name || "—"}</div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 260px), 1fr))", gap: "0 32px" }}>
        <div>
          <DetailField label={t("fSubtitle")}    value={tour.src_subtitle} />
          <DetailField label={t("fSummary")}     value={tour.src_summary} />
          <DetailField label={t("fDescription")} value={tour.src_description} />
          <DetailField label={t("fBestTime")}    value={tour.best_time_to_go} />
          <DetailField label={t("fFeature")}     value={tour.feature} />
        </div>
        <div>
          <DetailField label={t("fHighlights")} value={tour.src_highlights} />
          <DetailField label={t("fActivities")} value={tour.activities} />
          <DetailField label={t("fIncludes")}   value={tour.inclusions} />
          <DetailField label={t("fExcludes")}   value={tour.exclusions} />
          <DetailField label={t("fItinerary")}  value={itinPreview} />
          <DetailField label={t("fLinks")}      value={tour.links} />
        </div>
      </div>
    </div>
  );
}

// ─── Toast ────────────────────────────────────────────────────────────────────

function Toast({ msg, type }: { msg: string; type: "success" | "error" }) {
  return (
    <div style={{
      position: "fixed", bottom: 24, right: 24, zIndex: 999,
      background: type === "success" ? K.success : K.danger,
      color: KX.onSolid, padding: "12px 20px", borderRadius: 8,
      fontSize: 13, fontWeight: 500, boxShadow: KX.shadowMd,
      display: "flex", alignItems: "center", gap: 8,
    }}>
      {type === "success" ? <CheckCircle size={14} /> : <XCircle size={14} />}
      {msg}
    </div>
  );
}

// ─── Section: Tours Ready for Rewrite ─────────────────────────────────────────

const READY_PAGE_SIZE = 10;

function ToursReadySection({ tours, loading, onRefresh }: {
  tours: TourReadyItem[]; loading: boolean; onRefresh: () => void;
}) {
  const t = useT(UP);
  const relativeTime = useRelativeTime();
  const [page, setPage]               = useState(1);
  const [filterCountry, setFilterCountry] = useState("");
  const [filterFile, setFilterFile]   = useState("");
  const [showTrashed, setShowTrashed] = useState(false);
  const [trashing, setTrashing]       = useState<string | null>(null);
  const [restoring, setRestoring]     = useState<string | null>(null);
  const [trashedTours, setTrashedTours] = useState<TourReadyItem[]>([]);
  const [trashedLoading, setTrashedLoading] = useState(false);
  const [localTours, setLocalTours]   = useState<TourReadyItem[]>(tours);

  // A new `tours` prop (a refresh) replaces the local copy while rendering (was an effect, AA-1006).
  const [prevTours, setPrevTours] = useState(tours);
  if (tours !== prevTours) {
    setPrevTours(tours);
    setLocalTours(tours);
  }

  const uniqueCountries = Array.from(new Set(
    localTours.map(x => x.country).filter((c): c is string => Boolean(c))
  )).sort();
  const uniqueFiles = Array.from(new Set(
    localTours.map(x => x.filename).filter((f): f is string => Boolean(f))
  )).sort();

  const filtered = localTours.filter(x => {
    if (filterCountry && x.country !== filterCountry) return false;
    if (filterFile && x.filename !== filterFile) return false;
    return true;
  });
  const paginated = filtered.slice((page - 1) * READY_PAGE_SIZE, page * READY_PAGE_SIZE);

  function handleCountry(v: string) { setFilterCountry(v); setPage(1); }
  function handleFile(v: string)    { setFilterFile(v);    setPage(1); }

  async function loadTrashed() {
    setTrashedLoading(true);
    try {
      const res = await fetch("/api/admin/tours-trashed");
      if (res.ok) {
        const d = await res.json();
        setTrashedTours(d.tours || []);
      }
    } finally { setTrashedLoading(false); }
  }

  function toggleShowTrashed() {
    const next = !showTrashed;
    setShowTrashed(next);
    if (next) loadTrashed();
  }

  async function trashTour(tourId: string, tourName: string) {
    if (!confirm(t("confirmTrash", tourName))) return;
    setTrashing(tourId);
    try {
      const r = await fetch(`/api/admin/tours/${tourId}/trash`, { method: "PATCH" });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        alert(err.detail || t("trashFailed"));
        return;
      }
      setLocalTours(prev => prev.filter(x => x.tour_id !== tourId));
      if (showTrashed) {
        const trashed = localTours.find(x => x.tour_id === tourId);
        if (trashed) setTrashedTours(prev => [{ ...trashed, source_status: "trashed" }, ...prev]);
      }
    } finally { setTrashing(null); }
  }

  async function restoreTour(tourId: string, tourName: string) {
    setRestoring(tourId);
    try {
      const r = await fetch(`/api/admin/tours/${tourId}/restore`, { method: "PATCH" });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        alert(err.detail || t("restoreFailed"));
        return;
      }
      setTrashedTours(prev => prev.filter(x => x.tour_id !== tourId));
      onRefresh();
    } finally { setRestoring(null); }
  }

  return (
    <Section padded={false} style={{ marginTop: 32 }}>
      <div style={{
        padding: "14px 20px", borderBottom: `1px solid ${K.line}`,
        display: "flex", alignItems: "center", justifyContent: "space-between",
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span style={{ fontFamily: serif, fontSize: 16, fontWeight: 500, color: K.ink }}>
            {t("toursReady")}
          </span>
          {!loading && (
            <span style={{
              fontSize: 12, background: K.accentTint, color: K.accent,
              padding: "2px 10px", borderRadius: 10, fontWeight: 700,
            }}>{filtered.length}</span>
          )}
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <Button size="sm" variant={showTrashed ? "danger" : "ghost"} aria-pressed={showTrashed} onClick={toggleShowTrashed}>
            {showTrashed ? t("hideTrashed") : t("showTrashed")}
          </Button>
          <IconButton variant="plain" size="sm" onClick={onRefresh} label={t("refresh")}>
            <RefreshCw size={14} />
          </IconButton>
          <a href="/admin/s1-rewrite" style={{
            fontSize: 12, fontWeight: 600, color: K.accent,
            textDecoration: "none", display: "flex", alignItems: "center", gap: 4,
          }}>
            {t("goToS1")} <ArrowRight size={12} />
          </a>
        </div>
      </div>

      {/* Filters */}
      {!loading && localTours.length > 0 && (
        <div style={{ padding: "10px 20px", borderBottom: `1px solid ${K.line}`, display: "flex", gap: 10 }}>
          <Select value={filterCountry} onChange={e => handleCountry(e.target.value)}
            style={{ padding: "5px 8px", borderRadius: 6, border: `1px solid ${K.line}`, fontSize: 12, fontFamily: sans, background: K.card }}>
            <option value="">{t("allCountries")}</option>
            {uniqueCountries.map(c => <option key={c} value={c}>{c}</option>)}
          </Select>
          <Select value={filterFile} onChange={e => handleFile(e.target.value)}
            style={{ padding: "5px 8px", borderRadius: 6, border: `1px solid ${K.line}`, fontSize: 12, fontFamily: sans, background: K.card, maxWidth: 220 }}>
            <option value="">{t("allFiles")}</option>
            {uniqueFiles.map(f => <option key={f} value={f}>{stripUuidPrefix(f)}</option>)}
          </Select>
        </div>
      )}

      {loading ? (
        <div style={{ padding: 28, textAlign: "center", color: K.muted, fontSize: 13 }}>
          <Loader2 size={16} style={{ animation: "spin 1s linear infinite", marginRight: 8 }} />
          {t("loading")}
        </div>
      ) : localTours.length === 0 ? (
        <div style={{ padding: 28, textAlign: "center", color: K.muted, fontSize: 13 }}>
          {t("noToursReady")}
        </div>
      ) : filtered.length === 0 ? (
        <div style={{ padding: 28, textAlign: "center", color: K.muted, fontSize: 13 }}>
          {t("noToursMatch")}
        </div>
      ) : (
        <>
          <TableInset>
            <DataTable<TourReadyItem>
              tableId="upload-tours-ready"
              data={paginated}
              getRowId={(tr) => tr.tour_id}
              searchable={false}
              paginate={false}
              compact
              columns={[
                { id: "name", header: t("colTourName"), enableSorting: false, cell: ({ row: { original: tr } }) => <span style={{ fontWeight: 600, color: K.ink }}>{tr.src_name || "—"}</span> },
                { id: "country", header: t("colCountry"), enableSorting: false, cell: ({ row: { original: tr } }) => tr.country || "—" },
                { id: "file", header: t("colSourceFile"), enableSorting: false, cell: ({ row: { original: tr } }) => <span style={{ color: K.muted, fontSize: 12 }}>{tr.filename ? stripUuidPrefix(tr.filename) : "—"}</span> },
                { id: "ingested", header: t("colIngestedAt"), enableSorting: false, cell: ({ row: { original: tr } }) => <span style={{ color: K.muted, fontSize: 12 }}>{relativeTime(tr.ingest_at)}</span> },
                { id: "action", header: t("colAction"), enableSorting: false, cell: ({ row: { original: tr } }) => <><a href={`/admin/s1-rewrite?tour_id=${tr.tour_id}`} style={{
                          fontSize: 12, fontWeight: 600, color: K.accent,
                          textDecoration: "none", display: "flex", alignItems: "center", gap: 4,
                        }}>
                          {t("rewrite")} <ArrowRight size={11} />
                        </a></> },
                { id: "trash", header: "", enableSorting: false, cell: ({ row: { original: tr } }) => (
                        <Button size="sm" variant="danger"
                          onClick={() => trashTour(tr.tour_id, tr.src_name || t("thisTour"))}
                          disabled={trashing === tr.tour_id}
                          title={t("trashTitle")}
                        >
                          <Trash2 size={11} />
                          {trashing === tr.tour_id ? "…" : t("trash")}
                        </Button>) },
              ]}
            />
          </TableInset>
          {filtered.length > READY_PAGE_SIZE && (
            <div style={{ padding: "12px 20px", borderTop: `1px solid ${K.line}`, display: "flex", justifyContent: "flex-end" }}>
              <Pagination page={page} total={filtered.length} pageSize={READY_PAGE_SIZE} onPage={setPage} />
            </div>
          )}
        </>
      )}

      {/* Trashed tours section */}
      {showTrashed && (
        <div style={{ borderTop: `2px dashed ${K.dangerBorder}`, marginTop: 0 }}>
          <div style={{ padding: "10px 20px", background: K.dangerTint, display: "flex", alignItems: "center", gap: 8 }}>
            <Trash2 size={13} style={{ color: K.danger }} />
            <span style={{ fontSize: 12, fontWeight: 600, color: K.danger }}>
              {t("trashedTours")} {!trashedLoading && `(${trashedTours.length})`}
            </span>
          </div>
          {trashedLoading ? (
            <div style={{ padding: 20, textAlign: "center", color: K.muted, fontSize: 13 }}>
              <Loader2 size={14} style={{ animation: "spin 1s linear infinite" }} /> {t("loading")}
            </div>
          ) : trashedTours.length === 0 ? (
            <div style={{ padding: 20, textAlign: "center", color: K.muted, fontSize: 13 }}>{t("noTrashed")}</div>
          ) : (
            <TableInset>
              <DataTable<TourReadyItem>
                tableId="upload-trashed"
                data={trashedTours}
                getRowId={(tr) => tr.tour_id}
                searchable={false}
                paginate={false}
                compact
                columns={[
                  { id: "name", header: t("colTourName"), enableSorting: false, cell: ({ row: { original: tr } }) => <span style={{ color: K.danger, fontWeight: 500 }}>{tr.src_name || "—"}</span> },
                  { id: "country", header: t("colCountry"), enableSorting: false, cell: ({ row: { original: tr } }) => tr.country || "—" },
                  { id: "trashedAt", header: t("colTrashedAt"), enableSorting: false, cell: ({ row: { original: tr } }) => <span style={{ color: K.muted }}>{tr.deleted_at ? relativeTime(tr.deleted_at) : "—"}</span> },
                  { id: "restore", header: "", enableSorting: false, cell: ({ row: { original: tr } }) => <><Button size="sm" variant="ghost"
                          onClick={() => restoreTour(tr.tour_id, tr.src_name || t("thisTour"))}
                          disabled={restoring === tr.tour_id}
                          title={t("restoreTitle")}
                        >
                          <RotateCcw size={11} />
                          {restoring === tr.tour_id ? "…" : t("restore")}
                        </Button></> },
                ]}
              />
            </TableInset>
          )}
        </div>
      )}
    </Section>
  );
}

// ─── Section: Upload History ───────────────────────────────────────────────────

const HISTORY_PAGE_SIZE = 10;

function UploadHistorySection({ history, loading, onRefresh }: {
  history: UploadHistoryItem[]; loading: boolean; onRefresh: () => void;
}) {
  const t = useT(UP);
  const relativeTime = useRelativeTime();
  const [copied, setCopied]       = useState<string | null>(null);
  const [page, setPage]           = useState(1);
  const [dateFilter, setDateFilter] = useState("all");
  const [search, setSearch]       = useState("");

  function copyBatchId(id: string) {
    navigator.clipboard.writeText(id).catch(() => {});
    setCopied(id);
    setTimeout(() => setCopied(null), 1500);
  }

  // The time the filter compares against: read once (an impure call is not allowed while rendering).
  const [now] = useState(() => Date.now());
  const filtered = history.filter(h => {
    if (search && !stripUuidPrefix(h.filename).toLowerCase().includes(search.toLowerCase())) return false;
    if (dateFilter === "today" && (!h.parsed_at || now - new Date(h.parsed_at).getTime() > 86400000)) return false;
    if (dateFilter === "week"  && (!h.parsed_at || now - new Date(h.parsed_at).getTime() > 7 * 86400000)) return false;
    return true;
  });
  const paginated = filtered.slice((page - 1) * HISTORY_PAGE_SIZE, page * HISTORY_PAGE_SIZE);

  function handleSearch(v: string)   { setSearch(v);      setPage(1); }
  function handleDate(v: string)     { setDateFilter(v);  setPage(1); }

  return (
    <Section padded={false} style={{ marginTop: 20 }}>
      <div style={{
        padding: "14px 20px", borderBottom: `1px solid ${K.line}`,
        display: "flex", alignItems: "center", justifyContent: "space-between",
      }}>
        <span style={{ fontFamily: serif, fontSize: 16, fontWeight: 500, color: K.ink }}>
          {t("uploadHistory")}
        </span>
        <IconButton variant="plain" size="sm" onClick={onRefresh} label={t("refresh")}>
          <RefreshCw size={14} />
        </IconButton>
      </div>

      {/* Filters */}
      {!loading && history.length > 0 && (
        <div style={{ padding: "10px 20px", borderBottom: `1px solid ${K.line}`, display: "flex", gap: 10, alignItems: "center" }}>
          <Select value={dateFilter} onChange={e => handleDate(e.target.value)}
            style={{ padding: "5px 8px", borderRadius: 6, border: `1px solid ${K.line}`, fontSize: 12, fontFamily: sans, background: K.card }}>
            <option value="all">{t("allTime")}</option>
            <option value="today">{t("today")}</option>
            <option value="week">{t("last7")}</option>
          </Select>
          <div style={{ display: "flex", alignItems: "center", gap: 6, border: `1px solid ${K.line}`, borderRadius: 6, padding: "5px 8px", background: K.card, flex: 1, maxWidth: 260 }}>
            <Search size={12} style={{ color: K.muted2, flexShrink: 0 }} />
            <Input
              placeholder={t("searchFilename")}
              value={search}
              onChange={e => handleSearch(e.target.value)}
              style={{ border: "none", outline: "none", fontSize: 12, fontFamily: sans, width: "100%", background: "transparent" }}
            />
          </div>
        </div>
      )}

      {loading ? (
        <div style={{ padding: 28, textAlign: "center", color: K.muted, fontSize: 13 }}>
          <Loader2 size={16} style={{ animation: "spin 1s linear infinite", marginRight: 8 }} />
          {t("loading")}
        </div>
      ) : history.length === 0 ? (
        <div style={{ padding: 28, textAlign: "center", color: K.muted, fontSize: 13 }}>
          {t("noUploads")}
        </div>
      ) : filtered.length === 0 ? (
        <div style={{ padding: 28, textAlign: "center", color: K.muted, fontSize: 13 }}>
          {t("noUploadsMatch")}
        </div>
      ) : (
        <>
          <TableInset>
            <DataTable<UploadHistoryItem>
              tableId="upload-history"
              data={paginated}
              getRowId={(h) => h.id}
              searchable={false}
              paginate={false}
              compact
              columns={[
                { id: "file", header: t("colFileName"), enableSorting: false, cell: ({ row: { original: h } }) => <span style={{ maxWidth: 240, overflow: "hidden",
                        textOverflow: "ellipsis", whiteSpace: "nowrap", fontWeight: 500 }}>{stripUuidPrefix(h.filename)}</span> },
                { id: "size", header: t("colFileSize"), enableSorting: false, meta: { align: "right" }, cell: ({ row: { original: h } }) => <span style={{ color: K.muted }}>{h.file_size_kb ? `${h.file_size_kb.toFixed(0)} KB` : "—"}</span> },
                { id: "parsed", header: t("colParsedLanded"), enableSorting: false, meta: { align: "right" }, cell: ({ row: { original: h } }) => <>{/* AA-344: row_count is PARSED only — rows_landed (from
                            pipeline_runs.ingest_details) is what actually landed in raw_tours.
                            null rows_landed = pre-migration-091 source, show parsed count alone. */}
                        {h.rows_landed !== null
                          ? `${h.row_count ?? "—"} / ${h.rows_landed}`
                          : (h.row_count ?? "—")}</> },
                { id: "dropped", header: t("colDropped"), enableSorting: false, meta: { align: "right" }, cell: ({ row: { original: h } }) => <>{h.rows_dropped !== null && h.rows_dropped > 0
                          ? <Badge tone="danger">{t("droppedN", h.rows_dropped)}</Badge>
                          : <span style={{ color: K.muted2, fontSize: 12 }}>
                              {h.rows_dropped === 0 ? "0" : "—"}
                            </span>}</> },
                { id: "errors", header: t("colParseErrors"), enableSorting: false, meta: { align: "right" }, cell: ({ row: { original: h } }) => <>{h.parse_error_count > 0
                          ? <Badge tone="danger">{t("errorsN", h.parse_error_count)}</Badge>
                          : <span style={{ color: K.muted2, fontSize: 12 }}>0</span>}</> },
                { id: "uploaded", header: t("colUploadedAt"), enableSorting: false, meta: { align: "right" }, cell: ({ row: { original: h } }) => <span style={{ color: K.muted, fontSize: 12 }}>{relativeTime(h.parsed_at)}</span> },
                { id: "batch", header: t("colBatchId"), enableSorting: false, meta: { align: "right" }, cell: ({ row: { original: h } }) => <>{h.batch_id ? (
                          <LinkButton tone="inherit" size="sm" weight="regular"
                            onClick={() => copyBatchId(h.batch_id!)}
                            title={t("copyBatch")}
                            style={{ gap: 4, fontFamily: mono, fontSize: 11, color: copied === h.batch_id ? K.success : K.muted2 }}
                          >
                            {h.batch_id.slice(0, 8)}…
                            <Copy size={10} />
                          </LinkButton>
                        ) : "—"}</> },
              ]}
            />
          </TableInset>
          {filtered.length > HISTORY_PAGE_SIZE && (
            <div style={{ padding: "12px 20px", borderTop: `1px solid ${K.line}`, display: "flex", justifyContent: "flex-end" }}>
              <Pagination page={page} total={filtered.length} pageSize={HISTORY_PAGE_SIZE} onPage={setPage} />
            </div>
          )}
        </>
      )}
    </Section>
  );
}

// ─── DuplicateReview (Step 5) ─────────────────────────────────────────────────

type Decision = "bypass" | "replace" | "keep_both" | "restore" | "create_new";

// Message keys (messages.ts).
const DECISION_LABELS: Record<"bypass" | "replace" | "keep_both", "decBypass" | "decReplace" | "decKeepBoth"> = {
  bypass:    "decBypass",
  replace:   "decReplace",
  keep_both: "decKeepBoth",
};

// AA-986 L2 (Q6, ADR 0015) — a re-upload of a tour deleted at S0/A0 never revives it silently: staff choose to
// restore the deleted tour (tenants that were warned are told it is back) or to start a new tour from the row.
const DELETED_DECISION_LABELS: Record<"bypass" | "restore" | "create_new", "decBypass" | "decRestore" | "decCreateNew"> = {
  bypass:     "decBypass",
  restore:    "decRestore",
  create_new: "decCreateNew",
};

function decisionLabels(
  item: StagingItem,
): Record<string, "decBypass" | "decReplace" | "decKeepBoth" | "decRestore" | "decCreateNew"> {
  return item.matched_deleted ? DELETED_DECISION_LABELS : DECISION_LABELS;
}

function DuplicateReviewStep({
  batchIds,
  onDone,
}: {
  batchIds: string[];
  onDone: () => void;
}) {
  const t = useT(UP);
  const relativeTime = useRelativeTime();
  const [items, setItems]         = useState<StagingItem[]>([]);
  const [loading, setLoading]     = useState(true);
  const [decisions, setDecisions] = useState<Record<string, Decision>>({});
  const [submitting, setSubmitting] = useState(false);
  const [submitted, setSubmitted] = useState<Record<string, boolean>>({});
  const [error, setError]         = useState("");

  useEffect(() => {
    async function fetchAll() {
      setLoading(true);
      const allItems: StagingItem[] = [];
      for (const batchId of batchIds) {
        try {
          const res = await fetch(`/api/admin/upload-staging/${batchId}`);
          if (res.ok) {
            const data = await res.json();
            allItems.push(...(data.items || []));
          }
        } catch { /* ignore */ }
      }
      setItems(allItems);
      const initial: Record<string, Decision> = {};
      allItems.forEach(it => { initial[it.staging_id] = "bypass"; });
      setDecisions(initial);
      setLoading(false);
    }
    fetchAll();
  }, [batchIds.join(",")]);

  async function confirmAll() {
    setSubmitting(true);
    setError("");
    const results: Record<string, boolean> = {};
    const failures: string[] = [];
    for (const item of items) {
      if (submitted[item.staging_id]) continue;
      const dec = decisions[item.staging_id] || "bypass";
      try {
        const res = await fetch(`/api/admin/upload-staging/${item.staging_id}/decide`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ decision: dec }),
        });
        results[item.staging_id] = res.ok;
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          failures.push(`${item.incoming.src_name || "—"}: ${typeof body.detail === "string" ? body.detail : res.status}`);
        }
      } catch {
        results[item.staging_id] = false;
      }
    }
    setSubmitted(prev => ({ ...prev, ...results }));
    const allOk = Object.values(results).every(Boolean);
    if (!allOk) setError(failures.length ? t("someFailedList", failures.join("; ")) : t("someFailed"));
    setSubmitting(false);
    if (allOk) onDone();
  }

  if (loading) {
    return (
      <Section>
        <div style={{ display: "flex", alignItems: "center", gap: 10, color: K.muted, fontSize: 13 }}>
          <Loader2 size={16} style={{ animation: "spin 1s linear infinite" }} />
          {t("loadingDups")}
        </div>
      </Section>
    );
  }

  if (items.length === 0) {
    return (
      <Section>
        <div style={{ color: K.success, fontSize: 14, fontWeight: 600, marginBottom: 16 }}>
          {t("noDups")}
        </div>
        <Button variant="secondary" onClick={onDone}>{t("backToUpload")}</Button>
      </Section>
    );
  }

  const allDecided = items.every(it => submitted[it.staging_id]);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <Section>
        <Eyebrow>{t("dupTitle", items.length)}</Eyebrow>
        <p style={{ fontSize: 12, color: K.muted, margin: "0 0 16px" }}>
          {t("dupIntro")}
        </p>

        {error && (
          <div style={{ color: K.danger, fontSize: 12, marginBottom: 12 }}>{error}</div>
        )}

        <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
          {items.map(item => (
            <div key={item.staging_id} style={{
              border: `1px solid ${submitted[item.staging_id] ? K.success : K.line}`,
              borderRadius: 10, overflow: "hidden",
            }}>
              {/* Header */}
              <div style={{
                padding: "10px 16px", background: K.bg,
                borderBottom: `1px solid ${K.line}`,
                display: "flex", alignItems: "center", justifyContent: "space-between",
              }}>
                <span style={{ fontWeight: 600, fontSize: 13, color: K.ink }}>
                  {item.incoming.src_name || "—"}
                </span>
                {submitted[item.staging_id] && (
                  <Badge tone="success">{t("done")}</Badge>
                )}
              </div>

              {item.matched_deleted && (
                <div style={{
                  padding: "8px 16px", background: K.dangerTint, borderBottom: `1px solid ${K.line}`,
                  fontSize: 12, color: K.danger, fontWeight: 600,
                }}>
                  {t("deletedOn", item.matched_deleted_at ? item.matched_deleted_at.slice(0, 10) : t("earlierDate"))}
                </div>
              )}

              {/* Side-by-side */}
              <div style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))" }}>
                {/* Incoming */}
                <div style={{ padding: "12px 16px", borderRight: `1px solid ${K.line}` }}>
                  <div style={{
                    fontSize: 10, fontWeight: 700, textTransform: "uppercase",
                    letterSpacing: "0.12em", color: K.accent, marginBottom: 8,
                  }}>
                    {t("incoming")}
                  </div>
                  {[
                    [t("lName"),     item.incoming.src_name],
                    [t("lCountry"),  item.incoming.country],
                    [t("lPrice"),    item.incoming.price_raw],
                    [t("lProvider"), item.incoming.provider],
                    [t("lSummary"),  item.incoming.src_summary
                      ? (item.incoming.src_summary.length > 200
                          ? item.incoming.src_summary.slice(0, 200) + "…"
                          : item.incoming.src_summary)
                      : null],
                  ].map(([label, value]) => (
                    value ? (
                      <div key={String(label)} style={{ marginBottom: 6 }}>
                        <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase",
                          letterSpacing: "0.1em", color: K.muted, marginBottom: 1 }}>{label}</div>
                        <div style={{ fontSize: 12, color: K.body }}>{value}</div>
                      </div>
                    ) : null
                  ))}
                </div>

                {/* Existing */}
                <div style={{ padding: "12px 16px" }}>
                  <div style={{
                    fontSize: 10, fontWeight: 700, textTransform: "uppercase",
                    letterSpacing: "0.12em", color: K.muted, marginBottom: 8,
                  }}>
                    {t("existing")}
                  </div>
                  {[
                    [t("lName"),       item.existing.src_name],
                    [t("lCountry"),    item.existing.country],
                    [t("lPrice"),      item.existing.price_raw],
                    [t("lProvider"),   item.existing.provider],
                    [t("lIngested"),   item.existing.ingest_at
                      ? relativeTime(item.existing.ingest_at) : null],
                    [t("lSummary"),    item.existing.src_summary
                      ? (item.existing.src_summary.length > 200
                          ? item.existing.src_summary.slice(0, 200) + "…"
                          : item.existing.src_summary)
                      : null],
                  ].map(([label, value]) => (
                    value ? (
                      <div key={String(label)} style={{ marginBottom: 6 }}>
                        <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase",
                          letterSpacing: "0.1em", color: K.muted, marginBottom: 1 }}>{label}</div>
                        <div style={{ fontSize: 12, color: K.body }}>{value}</div>
                      </div>
                    ) : null
                  ))}
                </div>
              </div>

              {/* Decision */}
              {!submitted[item.staging_id] && (
                <div style={{
                  padding: "10px 16px", borderTop: `1px solid ${K.line}`,
                  display: "flex", alignItems: "center", gap: 12,
                }}>
                  <span style={{ fontSize: 12, fontWeight: 600, color: K.muted, flexShrink: 0 }}>
                    {t("decision")}
                  </span>
                  <Select
                    value={decisions[item.staging_id] || "bypass"}
                    onChange={e => setDecisions(prev => ({
                      ...prev, [item.staging_id]: e.target.value as Decision,
                    }))}
                    style={{
                      padding: "5px 8px", borderRadius: 6,
                      border: `1px solid ${K.line}`, fontSize: 12,
                      fontFamily: sans, background: K.card, flex: 1,
                    }}
                  >
                    {(Object.entries(decisionLabels(item)) as [Decision, ReturnType<typeof decisionLabels>[string]][]).map(([val, label]) => (
                      <option key={val} value={val}>{t(label)}</option>
                    ))}
                  </Select>
                </div>
              )}
            </div>
          ))}
        </div>
      </Section>

      {!allDecided && (
        <div style={{ display: "flex", gap: 12 }}>
          <Button variant="secondary" onClick={onDone}>{t("skipLater")}</Button>
          <Button
            variant="primary"
            disabled={submitting}
            onClick={confirmAll}
            style={{
              background: K.accent, border: `1px solid ${K.accent}`,
              display: "flex", alignItems: "center", gap: 8,
              opacity: submitting ? 0.6 : 1,
            }}
          >
            {submitting
              ? <><Loader2 size={13} style={{ animation: "spin 1s linear infinite" }} /> {t("saving")}</>
              : <>{t("confirmDecisions", items.length)} <ArrowRight size={14} /></>}
          </Button>
        </div>
      )}

      {allDecided && (
        <Section>
          <div style={{ color: K.success, fontSize: 14, fontWeight: 600, marginBottom: 16,
            display: "flex", alignItems: "center", gap: 8 }}>
            <CheckCircle size={16} /> {t("allApplied")}
          </div>
          <div style={{ display: "flex", gap: 12 }}>
            <Button variant="secondary" onClick={onDone}>{t("backToUpload")}</Button>
            <a href="/admin/s1-rewrite" style={{
              display: "inline-flex", alignItems: "center", gap: 8,
              padding: "9px 18px", borderRadius: 8,
              background: K.accent, border: `1px solid ${K.accent}`,
              fontSize: 13, fontWeight: 600, color: KX.onSolid, textDecoration: "none",
            }}>
              {t("goToS1")} <ArrowRight size={14} />
            </a>
          </div>
        </Section>
      )}
    </div>
  );
}

// ─── Tab 1: Tour Content ──────────────────────────────────────────────────────

type Step = 1 | 2 | 3 | 4 | 5;

function TourContentTab() {
  const t = useT(UP);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const [step, setStep]         = useState<Step>(1);
  const [dragging, setDragging] = useState(false);
  const [fileStates, setFileStates] = useState<FileState[]>([]);
  const [fileError, setFileError]   = useState("");
  const [maxTours, setMaxTours]     = useState(50);
  const [expandedRow, setExpandedRow] = useState<string | null>(null);
  const [dupBatchIds, setDupBatchIds] = useState<string[]>([]);

  const [uploadHistory, setUploadHistory] = useState<UploadHistoryItem[]>([]);
  const [toursReady, setToursReady]       = useState<TourReadyItem[]>([]);
  const [refreshKey, setRefreshKey] = useState(0);
  // Which refresh each list was loaded for: a new refreshKey shows the loaders until its answers arrive (AA-1006, no
  // setState in the effect body).
  const [historyFor, setHistoryFor] = useState<number | null>(null);
  const [toursFor, setToursFor] = useState<number | null>(null);
  const historyLoading = historyFor !== refreshKey;
  const toursLoading = toursFor !== refreshKey;

  const [toast, setToast] = useState<{ msg: string; type: "success" | "error" } | null>(null);

  useEffect(() => {
    fetch("/api/admin/upload-history")
      .then(r => r.ok ? r.json() : { sources: [] })
      .then(d => setUploadHistory(d.sources || []))
      .catch(() => setUploadHistory([]))
      .finally(() => setHistoryFor(refreshKey));

    fetch("/api/admin/tours-ready")
      .then(r => r.ok ? r.json() : { tours: [] })
      .then(d => setToursReady(d.tours || []))
      .catch(() => setToursReady([]))
      .finally(() => setToursFor(refreshKey));
  }, [refreshKey]);

  function showToast(msg: string, type: "success" | "error") {
    setToast({ msg, type });
    setTimeout(() => setToast(null), 3500);
  }

  function updateFile(id: string, update: Partial<FileState>) {
    setFileStates(prev => prev.map(f => f.id === id ? { ...f, ...update } : f));
  }

  function addFiles(newFiles: File[]) {
    const valid = newFiles.filter(f => {
      if (!f.name.match(/\.xlsx$/i)) return false;
      if (f.size > 50 * 1024 * 1024) return false;
      return true;
    });
    if (valid.length === 0) {
      setFileError(t("onlyXlsx"));
      return;
    }
    setFileError("");
    setFileStates(prev => [
      ...prev,
      ...valid.map(f => ({
        id: `${f.name}-${Date.now()}-${Math.random()}`,
        file: f,
        status: "pending" as const,
        expanded: false,
      })),
    ]);
  }

  function onDrop(e: React.DragEvent) {
    e.preventDefault(); setDragging(false);
    addFiles(Array.from(e.dataTransfer.files));
  }

  function removeFile(id: string) {
    setFileStates(prev => prev.filter(f => f.id !== id));
  }

  function reset() {
    setStep(1); setFileStates([]); setFileError("");
    setExpandedRow(null); setDupBatchIds([]);
  }

  async function doUploadAndParse() {
    if (fileStates.length === 0) return;
    setStep(2);

    await Promise.all(fileStates.map(async (fs) => {
      try {
        updateFile(fs.id, { status: "uploading" });

        const urlRes = await fetch("/api/admin/upload-url", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ filename: fs.file.name, tenant_id: TENANT_ID }),
        });
        if (!urlRes.ok) {
          const e = await urlRes.json().catch(() => ({}));
          throw new Error(e.detail || t("uploadUrlFailed", urlRes.status));
        }
        const urlData: { upload_url: string; s3_key: string } = await urlRes.json();

        const putRes = await fetch(urlData.upload_url, {
          method: "PUT", body: fs.file,
          headers: { "Content-Type": fs.file.type || "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" },
        });
        if (!putRes.ok) throw new Error(t("s3Failed", putRes.status));

        updateFile(fs.id, { status: "parsing", s3Key: urlData.s3_key });

        const parseRes = await fetch("/api/admin/ingest-s3", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ s3_key: urlData.s3_key, tenant_id: TENANT_ID, max_tours: maxTours, dry_run: true }),
        });
        if (!parseRes.ok) {
          const e = await parseRes.json().catch(() => ({}));
          throw new Error(e.detail || t("parseFailed", parseRes.status));
        }
        const parseData: DryRunResponse = await parseRes.json();

        updateFile(fs.id, {
          status: parseData.status === "blocked" ? "blocked-file" : "parsed",
          parseResult: parseData,
        });
      } catch (err) {
        updateFile(fs.id, {
          status: "error",
          parseError: err instanceof Error ? err.message : t("uploadFailed"),
        });
      }
    }));

    setStep(3);
  }

  async function doCommit() {
    const toCommit = fileStates.filter(f => f.status === "parsed");
    if (toCommit.length === 0) return;

    // Atomically pre-set all statuses before rendering step 4
    setFileStates(prev => prev.map(f => {
      if (f.status === "parsed") {
        return { ...f, status: "committing" as const };
      }
      return f;
    }));
    setStep(4);

    const batchIdsWithStaged: string[] = [];
    // AA-488 Gap 2: distinguish "committed, but nothing new" from a real save — the toast (and
    // the per-file/aggregate UI below) used to say "success" unconditionally even when every
    // row in a file was already in the catalog (written=0, staged=0).
    let anySuccess  = false;
    let anyNewTours = false;

    await Promise.all(toCommit.map(async (fs) => {
      try {
        const res = await fetch("/api/admin/ingest-s3", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ s3_key: fs.s3Key, tenant_id: TENANT_ID, max_tours: maxTours, dry_run: false }),
        });
        if (!res.ok) {
          const e = await res.json().catch(() => ({}));
          throw new Error(e.detail || t("commitFailed", res.status));
        }
        const data: CommitResponse = await res.json();
        updateFile(fs.id, { status: "done", commitResult: data });
        anySuccess = true;
        const written = data.tours_written ?? data.tour_count ?? 0;
        const staged  = data.tours_staged ?? 0;
        if (written > 0 || staged > 0) anyNewTours = true;
        if (staged > 0 && data.batch_id) {
          batchIdsWithStaged.push(data.batch_id);
        }
      } catch (err) {
        updateFile(fs.id, {
          status: "error",
          commitError: err instanceof Error ? err.message : t("commitFailedShort"),
        });
      }
    }));

    setRefreshKey(k => k + 1);

    if (batchIdsWithStaged.length > 0) {
      setDupBatchIds(batchIdsWithStaged);
      setStep(5);
      showToast(t("toastDups"), "success");
    } else if (anySuccess && !anyNewTours) {
      showToast(t("toastNoNew"), "success");
    } else {
      showToast(t("toastSaved"), "success");
    }
  }

  const totalReady   = fileStates.reduce((n, f) => n + (f.parseResult?.ready_count ?? 0), 0);
  const totalBlocked = fileStates.reduce((n, f) => n + (f.parseResult?.blocked_count ?? 0), 0);
  // Files eligible for a real commit call — independent of ready_count, since doCommit()
  // now sends every "parsed" file to the backend and lets it decide (see AA-312).
  const commitFileCount  = fileStates.filter(f => f.status === "parsed").length;
  const hasFilesToCommit = commitFileCount > 0;
  const allUploading = fileStates.some(f => f.status === "uploading" || f.status === "parsing");
  const allDone      = fileStates.length > 0 && fileStates.every(f => ["done", "error", "blocked-file"].includes(f.status));
  // AA-488 Gap 2: a "done" file whose commit wrote nothing new (every row already in the
  // catalog) is a real outcome, not a failure — but it must not look like a success either.
  const fileHasNoNewTours = (fs: FileState) => {
    const written = fs.commitResult?.tours_written ?? fs.commitResult?.tour_count ?? 0;
    const staged  = fs.commitResult?.tours_staged ?? 0;
    return written === 0 && staged === 0;
  };
  const doneFiles = fileStates.filter(f => f.status === "done");
  const allDoneFilesHaveNoNewTours = doneFiles.length > 0 && doneFiles.every(fileHasNoNewTours);

  const fileStatusColor: Record<FileState["status"], string> = {
    pending: K.muted2, uploading: K.accent, parsing: K.accent,
    parsed: K.success, "blocked-file": K.danger, committing: K.accent, done: K.success, error: K.danger,
  };
  const fileStatusLabel: Record<FileState["status"], string> = {
    pending: t("stPending"), uploading: t("stUploading"), parsing: t("stParsing"),
    parsed: t("stReady"), "blocked-file": t("stBlocked"), committing: t("stSaving"), done: t("stSaved"), error: t("stError"),
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 0 }}>
      <StepIndicator step={step} />

      {/* ── Step 1: Select Files ── */}
      {step === 1 && (
        <div style={{ maxWidth: 620 }}>
          <Section>
            <Eyebrow>{t("sourceFiles")}</Eyebrow>
            <div
              onDragOver={e => { e.preventDefault(); setDragging(true); }}
              onDragLeave={() => setDragging(false)}
              onDrop={onDrop}
              onClick={() => fileInputRef.current?.click()}
              style={{
                border: `2px dashed ${dragging ? K.accent : K.line}`,
                borderRadius: 10, padding: "32px 24px", textAlign: "center",
                cursor: "pointer", transition: "all .15s", marginBottom: 16,
                background: dragging ? K.accentTint : K.bg,
              }}
            >
              <input ref={fileInputRef} type="file" accept=".xlsx" multiple style={{ display: "none" }}
                onChange={e => { if (e.target.files) addFiles(Array.from(e.target.files)); }} />
              <Upload size={26} style={{ color: K.muted2, marginBottom: 10 }} />
              <div style={{ fontSize: 14, fontWeight: 600, color: K.ink }}>
                {t("dropHere")}
              </div>
              <div style={{ fontSize: 12, color: K.muted2, marginTop: 4 }}>
                {t("supported")}
              </div>
            </div>

            {fileError && (
              <div style={{ display: "flex", alignItems: "center", gap: 8, color: K.danger, fontSize: 13, marginBottom: 12 }}>
                <XCircle size={14} />{fileError}
              </div>
            )}

            {/* File list */}
            {fileStates.length > 0 && (
              <div style={{ marginBottom: 16, display: "flex", flexDirection: "column", gap: 8 }}>
                {fileStates.map(fs => (
                  <div key={fs.id} style={{
                    display: "flex", alignItems: "center", gap: 10,
                    background: K.bg, borderRadius: 8, padding: "8px 12px",
                    border: `1px solid ${K.line}`,
                  }}>
                    <FileText size={14} style={{ color: K.muted2, flexShrink: 0 }} />
                    <span style={{ fontSize: 13, color: K.ink, flex: 1, minWidth: 0,
                      overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {fs.file.name}
                    </span>
                    <span style={{ fontSize: 11, color: K.muted2, flexShrink: 0 }}>
                      {(fs.file.size / 1024).toFixed(0)} KB
                    </span>
                    <IconButton variant="plain" size="xs" onClick={() => removeFile(fs.id)} label={t("removeFile", fs.file.name)}>
                      <XCircle size={14} />
                    </IconButton>
                  </div>
                ))}
              </div>
            )}

            <div style={{ display: "flex", alignItems: "center", gap: 16, marginBottom: 20 }}>
              <div>
                <label style={{ fontSize: 12, fontWeight: 600, color: K.muted, display: "block", marginBottom: 5 }}>
                  {t("maxTours")}
                </label>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <Input type="number" min={1} max={500} value={maxTours}
                    onChange={e => setMaxTours(Math.min(500, Math.max(1, Number(e.target.value))))}
                    style={{ width: 90, padding: "7px 10px", borderRadius: 7,
                      border: `1px solid ${K.line}`, background: K.card,
                      fontSize: 13, color: K.ink, fontFamily: sans }} />
                  <span style={{ fontSize: 11, color: K.muted2 }}>{t("minMax")}</span>
                </div>
              </div>
            </div>

            <Button variant="primary" disabled={fileStates.length === 0} onClick={doUploadAndParse}
              style={{
                background: fileStates.length > 0 ? K.accent : K.muted,
                border: `1px solid ${fileStates.length > 0 ? K.accent : K.muted}`,
                display: "flex", alignItems: "center", gap: 8,
              }}>
              {t("uploadFiles")} <ArrowRight size={14} />
            </Button>
          </Section>
        </div>
      )}

      {/* ── Step 2: Uploading ── */}
      {step === 2 && (
        <div style={{ maxWidth: 560 }}>
          <Section>
            <Eyebrow>{t("stepUpload")}</Eyebrow>
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              {fileStates.map(fs => (
                <div key={fs.id} style={{ display: "flex", alignItems: "center", gap: 12 }}>
                  <div style={{
                    width: 22, height: 22, borderRadius: "50%", flexShrink: 0,
                    display: "flex", alignItems: "center", justifyContent: "center",
                    background: fs.status === "done" || fs.status === "parsed" ? K.success
                      : fs.status === "error" ? K.danger : K.accent,
                  }}>
                    {fs.status === "uploading" || fs.status === "parsing"
                      ? <Loader2 size={12} style={{ color: KX.onSolid, animation: "spin 1s linear infinite" }} />
                      : fs.status === "error"
                        ? <XCircle size={12} style={{ color: KX.onSolid }} />
                        : <CheckCircle size={12} style={{ color: KX.onSolid }} />}
                  </div>
                  <span style={{ fontSize: 13, flex: 1, minWidth: 0,
                    overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                    color: fs.status === "error" ? K.danger : K.body }}>
                    {fs.file.name}
                  </span>
                  <span style={{ fontSize: 12, fontWeight: 500, color: fileStatusColor[fs.status], flexShrink: 0 }}>
                    {fileStatusLabel[fs.status]}
                  </span>
                </div>
              ))}
            </div>
            {allUploading && (
              <div style={{ marginTop: 16, color: K.muted, fontSize: 12 }}>
                {t("uploadingParallel")}
              </div>
            )}
          </Section>
        </div>
      )}

      {/* ── Step 3: Parse & Review ── */}
      {step === 3 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
          {/* Summary cards */}
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 200px), 1fr))", gap: 14 }}>
            {[
              { label: t("toursReadyCard"),   value: totalReady,   color: K.success },
              { label: t("toursBlockedCard"), value: totalBlocked, color: totalBlocked > 0 ? K.danger : K.muted2 },
              { label: t("filesCard"),         value: fileStates.length, color: K.accent },
            ].map(c => (
              <Section key={c.label}>
                <Eyebrow>{c.label}</Eyebrow>
                <div style={{ fontFamily: sans, fontVariantNumeric: "tabular-nums", fontSize: 28, fontWeight: 600,
                  color: c.color, letterSpacing: "-0.02em" }}>{c.value}</div>
              </Section>
            ))}
          </div>

          {/* Per-file results */}
          {fileStates.map(fs => (
            <Section padded={false} style={{ border: fs.status === "error" || fs.status === "blocked-file"
                ? `1px solid ${K.danger}` : undefined }} key={fs.id}>
              <div
                style={{
                  padding: "14px 20px", borderBottom: `1px solid ${K.line}`,
                  display: "flex", alignItems: "center", gap: 10,
                  cursor: "pointer",
                }}
                onClick={() => updateFile(fs.id, { expanded: !fs.expanded })}
              >
                {fs.status === "error" || fs.status === "blocked-file"
                  ? <XCircle size={14} style={{ color: K.danger }} />
                  : <CheckCircle size={14} style={{ color: K.success }} />}
                <span style={{ flex: 1, fontWeight: 600, fontSize: 14, color: K.ink }}>
                  {fs.file.name}
                </span>
                {fs.status === "parsed" && (
                  <>
                    <Badge tone="success">{t("readyN", fs.parseResult?.ready_count ?? 0)}</Badge>
                    {(fs.parseResult?.blocked_count ?? 0) > 0 && (
                      <Badge tone="danger">{t("blockedN", fs.parseResult?.blocked_count ?? 0)}</Badge>
                    )}
                  </>
                )}
                {fs.status === "blocked-file" && (
                  <Badge tone="danger">{t("fileBlocked")}</Badge>
                )}
                {fs.status === "error" && (
                  <Badge tone="danger">{t("error")}</Badge>
                )}
                {fs.expanded ? <ChevronUp size={14} style={{ color: K.muted2 }} /> : <ChevronDown size={14} style={{ color: K.muted2 }} />}
              </div>

              {fs.expanded && (
                <div style={{ padding: "0" }}>
                  {/* Error state */}
                  {(fs.status === "error" || fs.status === "blocked-file") && (
                    <div style={{ padding: "16px 20px", color: K.danger, fontSize: 13 }}>
                      {fs.parseError || fs.parseResult?.message || t("uploadFailed")}
                    </div>
                  )}

                  {/* Ready tours table */}
                  {fs.status === "parsed" && (fs.parseResult?.ready_count ?? 0) > 0 && (
                    <div>
                      <div style={{ padding: "10px 20px 4px", fontSize: 11, fontWeight: 700,
                        textTransform: "uppercase", letterSpacing: "0.12em", color: K.muted }}>
                        {t("readyTours")}
                      </div>
                      <TableInset>
                        <ReadyToursTable
                          tours={fs.parseResult?.tours ?? []}
                          expanded={expandedRow}
                          onToggle={(id) => setExpandedRow(prev => prev === id ? null : id)}
                        />
                      </TableInset>
                    </div>
                  )}

                  {/* Blocked tours table */}
                  {fs.status === "parsed" && (fs.parseResult?.blocked_count ?? 0) > 0 && (
                    <div>
                      <div style={{ padding: "10px 20px 4px", fontSize: 11, fontWeight: 700,
                        textTransform: "uppercase", letterSpacing: "0.12em", color: K.danger }}>
                        {t("blockedTours")}
                      </div>
                      <TableInset>
                        <DataTable<BlockedTour & { rowId: string }>
                          tableId="upload-parsed-blocked"
                          data={(fs.parseResult?.blocked_tours ?? []).map((bt, i) => ({ ...bt, rowId: String(i) }))}
                          getRowId={(bt) => bt.rowId}
                          searchable={false}
                          paginate={false}
                          compact
                          columns={[
                            { id: "name", header: t("colTourName"), enableSorting: false, cell: ({ row: { original: bt } }) => <span style={{ fontWeight: 600 }}>{bt.src_name}</span> },
                            { id: "country", header: t("colCountry"), enableSorting: false, cell: ({ row: { original: bt } }) => bt.country || "—" },
                            { id: "reason", header: t("colReason"), enableSorting: false, cell: ({ row: { original: bt } }) => <>{/* AA-490: duplicate_in_file (two rows in THIS upload sharing
                                      name+provider, mirrors the real Commit-time drop from
                                      AA-488 Gap 1) is now distinguished from duplicate_tour
                                      (already in the DB catalog) — was silently absent from
                                      preview before this fix. */}
                                  <Badge tone={
                                    bt.reason === "duplicate_tour"    ? "info"  :
                                    bt.reason === "duplicate_of_existing" ? "info" :
                                    bt.reason === "empty_itinerary"   ? "danger"   :
                                    bt.reason === "duplicate_in_file" ? "warning" : "warning"
                                  }>
                                    {bt.reason === "duplicate_tour"    ? t("rDuplicate") :
                                     bt.reason === "duplicate_in_file" ? t("rDuplicateInFile") :
                                     /* AA-604: no itinerary body → cannot be rewritten (POI/activity
                                        or source file lacking itinerary content) */
                                     bt.reason === "empty_itinerary"   ? t("rNoItinerary") :
                                     bt.reason === "duplicate_of_existing" ? t("rNearDuplicate") :
                                     bt.reason === "not_a_tour"        ? t("rNotATour") :
                                     bt.reason === "thin_itinerary"    ? t("rThinItinerary") :
                                                                         t("rMissingFields")}
                                  </Badge></> },
                            { id: "details", header: t("colDetails"), enableSorting: false, cell: ({ row: { original: bt } }) => <span style={{ fontSize: 12, color: K.muted }}>{bt.reason === "missing_fields"
                                    ? (bt.missing_fields ?? []).join(", ")
                                    : bt.message || t("alreadyInCatalog")}</span> },
                          ]}
                        />
                      </TableInset>
                    </div>
                  )}
                </div>
              )}
            </Section>
          ))}

          {/* Action buttons */}
          <div style={{ display: "flex", gap: 12 }}>
            <Button variant="secondary" onClick={reset}>{t("uploadAnother")}</Button>
            <Button
              variant="primary"
              disabled={!hasFilesToCommit}
              onClick={doCommit}
              style={{
                background: hasFilesToCommit ? K.accent : K.muted,
                border: `1px solid ${hasFilesToCommit ? K.accent : K.muted}`,
                display: "flex", alignItems: "center", gap: 8,
                opacity: hasFilesToCommit ? 1 : 0.5,
                cursor: hasFilesToCommit ? "pointer" : "not-allowed",
              }}
            >
              {t("confirmSave", commitFileCount)} <ArrowRight size={14} />
            </Button>
          </div>
        </div>
      )}

      {/* ── Step 4: Commit ── */}
      {step === 4 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
          <Section>
            <Eyebrow>{t("savingDb")}</Eyebrow>
            <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
              {fileStates.filter(f => f.status !== "blocked-file").map(fs => {
                const noNewTours = fs.status === "done" && fileHasNoNewTours(fs);
                return (
                <div key={fs.id} style={{ display: "flex", alignItems: "center", gap: 12 }}>
                  <div style={{
                    width: 22, height: 22, borderRadius: "50%", flexShrink: 0,
                    display: "flex", alignItems: "center", justifyContent: "center",
                    background: fs.status === "done" ? (noNewTours ? K.accent : K.success)
                      : fs.status === "error" ? K.danger : K.accent,
                  }}>
                    {fs.status === "committing"
                      ? <Loader2 size={12} style={{ color: KX.onSolid, animation: "spin 1s linear infinite" }} />
                      : fs.status === "error"
                        ? <XCircle size={12} style={{ color: KX.onSolid }} />
                        : noNewTours
                          ? <AlertCircle size={12} style={{ color: KX.onSolid }} />
                          : <CheckCircle size={12} style={{ color: KX.onSolid }} />}
                  </div>
                  <span style={{ fontSize: 13, flex: 1, color: K.body }}>{fs.file.name}</span>
                  <span style={{ fontSize: 12, fontWeight: 600,
                    color: fs.status === "done" && noNewTours ? K.accent : fileStatusColor[fs.status] }}>
                    {fs.status === "done"
                      ? (() => {
                          const written = fs.commitResult?.tours_written ?? fs.commitResult?.tour_count ?? 0;
                          const staged  = fs.commitResult?.tours_staged ?? 0;
                          if (staged > 0) return t("savedNeedReview", written, staged);
                          if (written === 0) return t("noNewAllDups");
                          return t("toursSaved", written);
                        })()
                      : fileStatusLabel[fs.status]}
                  </span>
                  {fs.commitError && (
                    <span style={{ fontSize: 11, color: K.danger }}>{fs.commitError}</span>
                  )}
                </div>
                );
              })}
            </div>

            {allDone && dupBatchIds.length === 0 && allDoneFilesHaveNoNewTours && (
              <div style={{ marginTop: 20 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8,
                  color: K.accent, fontSize: 14, fontWeight: 600, marginBottom: 16 }}>
                  <AlertCircle size={16} />
                  {t("toastNoNew")}
                </div>
                <div style={{ display: "flex", gap: 12 }}>
                  <Button variant="secondary" onClick={reset}>{t("uploadMore")}</Button>
                </div>
              </div>
            )}

            {allDone && dupBatchIds.length === 0 && !allDoneFilesHaveNoNewTours && (
              <div style={{ marginTop: 20 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8,
                  color: K.success, fontSize: 14, fontWeight: 600, marginBottom: 16 }}>
                  <CheckCircle size={16} />
                  {t("doneReady")}
                </div>
                <div style={{ display: "flex", gap: 12 }}>
                  <Button variant="secondary" onClick={reset}>{t("uploadMore")}</Button>
                  <a href="/admin/s1-rewrite" style={{
                    display: "inline-flex", alignItems: "center", gap: 8,
                    padding: "9px 18px", borderRadius: 8,
                    background: K.accent, border: `1px solid ${K.accent}`,
                    fontSize: 13, fontWeight: 600, color: KX.onSolid, textDecoration: "none",
                  }}>
                    {t("goToS1")} <ArrowRight size={14} />
                  </a>
                </div>
              </div>
            )}
          </Section>
        </div>
      )}

      {/* ── Step 5: Duplicate Review ── */}
      {step === 5 && (
        <DuplicateReviewStep
          batchIds={dupBatchIds}
          onDone={reset}
        />
      )}

      {/* ── Always-visible sections ── */}
      <ToursReadySection
        tours={toursReady}
        loading={toursLoading}
        onRefresh={() => setRefreshKey(k => k + 1)}
      />
      <UploadHistorySection
        history={uploadHistory}
        loading={historyLoading}
        onRefresh={() => setRefreshKey(k => k + 1)}
      />

      {toast && <Toast msg={toast.msg} type={toast.type} />}
    </div>
  );
}

// ─── Main page ────────────────────────────────────────────────────────────────

export default function UploadPage() {
  const t = useT(UP);
  return (
        <PageFrame>
          <PageHeader
            title={t("title")}
            description={
              <>
                <span style={{ display: "block", marginBottom: 4 }}>{t("subtitle")}</span>
                <span style={{ display: "block", fontSize: 12, color: K.muted2 }}>
                  {t("brandLead")}{" "}
                  <a href="/admin/settings?tab=brand" style={{ color: K.accent, textDecoration: "none", fontWeight: 500 }}>
                    {t("brandLink")}
                  </a>
                </span>
              </>
            }
          />
          <TourContentTab />
        </PageFrame>
  );
}
