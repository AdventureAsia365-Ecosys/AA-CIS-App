"use client";
// app/admin/tenant-activity/page.tsx — AA-568, replaces AA-551's 3-tab page (Write/Gate/Review/
// Publish) with ONE merged "06 · Content Trace" table.
//
// STEP0 (this issue's own Linear comment, 09/09/2026 — full trace docs/implementation-notes/
// AA-568.md) confirmed no gap blocks this build: Angle (all 3 + chosen flag) and Gate (specific
// violation text, not just counts) are both fully available on `GET /admin/a4/content-log`.
// Retry has REAL data (which gate blocked a round + the exact violation text fed back to the
// writer, from `repair_log`) but NOT a content diff — the intermediate attempt's prose was never
// persisted anywhere (traced to `services/acp_content_writing/service.py`'s write loop, which
// overwrites one `content_text` variable per attempt). Per Nghiệp's confirmed default (STEP0
// comment), retries below are labeled "Retry reason" / "what was fed back to the writer to
// rewrite", never "what changed" — a real content-diff view is AA-570's separate scope.
//
// AA-572 follow-up fixes (found by Nghiệp's own post-Done screenshots, not caught before this
// page was marked Done — see that issue for the full list): (1) "Round N — Lý do yêu cầu viết
// lại" label was never translated — fixed to "Round N — Retry reason", the ONLY UI string in
// this file that was still Vietnamese (grepped the whole file + auditPanels.tsx/adminUi.tsx for
// diacritics first — everything else was already English or a code comment, not UI text); (2)
// tenant stat header (total pieces + per-channel breakdown, computed client-side from the
// already-loaded `rows`, no new endpoint); (3) sortable Gate count/Retry count/Created at
// columns; (4) Content block's fixed `maxHeight`/`overflowY` scrollbox removed — full text now
// renders inline; (5) Lineage split into one line per entity; (6) sticky `<thead>` — the real
// root cause was NOT a missing `minHeight:0`/`height:"100vh"` (both were already correct on this
// page's own scroll container) but a REDUNDANT `overflowX:"auto"` on the table's own wrapper div
// — per the CSS overflow spec, setting overflow-x to anything but visible while overflow-y stays
// visible forces overflow-y to compute to auto too, making that inner div its own (non-scrolling,
// since its height is unconstrained) sticky containing block instead of the real outer scrollport
// — so `position:sticky` on `<th>` had nothing real to stick to. Fixed by moving `overflowX:
// "auto"` onto the actual outer scroll container and letting the inner table wrapper stay
// `overflow: visible` (default).
//
// This page also confirms AA-558's finding for real: the old "06 Write/Gate" and "07 Review" tabs
// called the exact same endpoint (`/admin/a4/content-log`) for the exact same rows, just under 2
// tab labels — merging removes that literal duplication, not just a UI simplification. "08
// Publish" (`/admin/a4/publish-log`) is now folded in too: AA-568 extended `content-log` itself
// to carry `publish_external_url`/`publish_published_at` so this page needs exactly ONE backend
// call, not three.
//
// Per-tenant, cross-tenant-by-default (A4 pattern, AA-437) — Tenant filter defaults to "All
// tenants", never hard-scoped to one. Admin-only route (unchanged from AA-551,
// proxy.ts PROTECTED_ROUTES already allowlists `/admin/tenant-activity`), genuinely a peer
// of 01-05 in Social Content.
//
// AA-575 — this page never actually rendered that 01-06 sub-nav: it was JSX embedded directly in
// atom-curation/page.tsx's own render tree, and this page (a separate route) had no equivalent
// markup at all — so navigating here (by click OR direct URL) made the sub-nav disappear
// entirely, with no way back except browser Back. Fixed by extracting it into
// `_components/SocialContentSubNav.tsx`, now mounted on both pages with "06" highlighted here.
// AA-575 also extends AA-572's Gate/Retry/Created-at-only sort to every column — see `sortValue()`
// below.
//
// AA-560 — "Force unpublish" added to the Publish section of the row accordion (published rows
// only). This is the old `/admin/a4-oversight` (Cross-Tenant Oversight) page's one mutating
// action, moved here after a STEP0 gap-check found that page's Publish Log section's WRITE half
// (unlike its READ half) had no other home — Content Trace previously only showed publish_status
// read-only. Calls the same untouched `POST /admin/a4/publish-log/{id}/unpublish` (AA-455).
// `admin_a4.py::get_content_log()` now also returns `publish_id` (previously computed into
// `publish_status` and discarded) so this button can address the right row.
//
// AA-1006 follow-up (S263 F3) — paged on the server (lib/contentTrace.ts): the page loaded only the newest 200 rows
// and its "Total pieces" tile counted those (always 200); it now asks for one page (`limit` / `offset`, 50 a page by
// default), and the header tiles read the API's own `total` / `by_channel` for the filters. A column sort orders the
// rows of the page on screen. Seven columns instead of ten so the table fits its pane at 1440 (it scrolled sideways
// from Status on, with Topic and Angle squeezed): Tenant / tour in one column, the publish state under Status, the
// retries under Gates; Topic and Angle keep a readable minimum width and every text column wraps.
import { useState, useCallback, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { Radio } from "lucide-react";
import SocialContentSubNav, { SocialContentSplit } from "../_components/SocialContentSubNav";
import { fetchJson, EmptyState, ErrorState } from "../_components/auditPanels";
import { TenantSelect } from "../_components/TenantSelect";
import {
  PageFrame, PageFrameBody, PageFrameHeader, PageHeader, useFormat, useT, Badge, K, LoadingScreen, Section, mono,
  sans, type Tone, Button, DataTable, Drawer, Input, Select, type ColumnDef,
} from "../../_kit";
import { CHANNEL, TRACE } from "./messages";
import {
  pageFor, traceFilterQuery, traceQuery, traceSortValue, traceStats, TRACE_DEFAULT_PAGE_SIZE, TRACE_PAGE_SIZES,
  type TraceAnswer, type TracePage, type TraceSortKey,
} from "@/lib/contentTrace";
import { shownRange } from "@/lib/pageRange";

// ── Types — match GET /api/admin/a4/content-log's response (AA-568 extension) ──────────────────

interface AngleOption {
  option_id: string; idx: number; name: string; why_it_works: string; formula_fit: string;
  best_final_style: string; recommended: boolean; chosen: boolean;
}
interface GateEntry { gate?: string; passed?: boolean; violations?: string[]; repairable?: boolean; blocking?: boolean; }
interface RepairRound { attempt?: number; gate_targeted?: string; violations?: string[]; repairable?: boolean; }
type ContentSource =
  | { kind: "segment"; segment_id: string; place: string | null; action: string | null }
  | { kind: "route"; route_id: string; hub_name: string | null; first_day: number | null; last_day: number | null }
  | { kind: "direct_atom" };

interface ContentLogRow {
  piece_id: string; tenant_id: string; tenant_name: string | null; tenant_slug: string | null;
  goal: string | null; topic: string; channel: string; status: string; held_reason: string | null;
  gate_ledger: GateEntry[]; gate_pass_count: number; gate_total_count: number;
  repair_log: RepairRound[]; retry_count: number; attempt_number: number;
  content_text: string; cta: string | null; angles: AngleOption[];
  atom: { text: string; activity_type: string | null; emotional_hook: string | null; season_note: string | null } | null;
  tour: { name: string; destination: string | null } | null;
  source: ContentSource; is_buffer_retry: boolean; sibling_piece_count: number;
  publish_status: "published" | "pending_publish" | "n/a";
  // AA-560 — publish_log's own row id, needed to address the "Force unpublish" action below.
  // null whenever publish_status !== "published" (no publish_log row exists yet).
  publish_id: string | null;
  publish_external_url: string | null; publish_published_at: string | null;
  // AA-687 — the t9_write job that wrote this piece (null before AA-652 / buffer-retry pieces).
  job_id: string | null;
  created_at: string;
}

const STATUS_COLOR: Record<string, Tone> = {
  approved: "success", held: "warning", processing: "neutral", failed: "danger",
};

const STATUS_OPTIONS = [
  { value: "", label: "allStatuses" },
  { value: "processing", label: "processing" },
  { value: "approved", label: "approved" },
  { value: "held", label: "held" },
  { value: "failed", label: "failed" },
] as const;

// Real channel values only (SlateTab.tsx's own CHANNEL_TABS — the one place this codebase
// enumerates every channel a piece can actually be written for), never a fabricated list.
const CHANNEL_OPTIONS = [
  { value: "", label: "All channels" },
  { value: "blog", label: "Blog" },
  { value: "linkedin", label: "LinkedIn" },
  { value: "facebook", label: "Facebook" },
  { value: "instagram", label: "Instagram" },
  { value: "tiktok", label: "TikTok" },
  { value: "email", label: "Email" },
  { value: "landing_page", label: "Landing Page" },
  { value: "ads", label: "Ads" },
];

const PUBLISHED_OPTIONS = [
  { value: "", label: "any" },
  { value: "yes", label: "published" },
  { value: "no", label: "notPublished" },
] as const;

const selectStyle: React.CSSProperties = {
  padding: "8px 12px", background: K.card, border: `1px solid ${K.line}`, borderRadius: 8,
  fontSize: 12.5, fontFamily: sans, color: K.body, cursor: "pointer",
};

const CHANNEL_LABEL: Record<string, string> = Object.fromEntries(
  CHANNEL_OPTIONS.filter(o => o.value).map(o => [o.value, o.label]),
);

/** The channel filter label in the UI language ("" = all channels); an unknown code as it is. */
function useChannelLabel(): (code: string) => string {
  const t = useT(CHANNEL) as unknown as (key: string) => string;
  return (code) => (!code ? t("all") : Object.hasOwn(CHANNEL.en, code) ? t(code) : code);
}

export default function ContentTracePage() {
  const t = useT(TRACE);
  const channelLabel = useChannelLabel();
  const [tenantId, setTenantId] = useState("");
  const [channel, setChannel] = useState("");
  const [status, setStatus] = useState("");
  const [published, setPublished] = useState("");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");

  // AA-1006: the row detail opens in a kit Drawer (the table row used to expand in place); sorting is the kit table's.
  const [openRow, setOpenRow] = useState<ContentLogRow | null>(null);
  // AA-560 — Force unpublish state, same shape a4-oversight/page.tsx's own PublishLogSection used.
  const [unpublishingId, setUnpublishingId] = useState<string | null>(null);
  const [unpublishError, setUnpublishError] = useState<string | null>(null);

  const filterQuery = useMemo(
    () => traceFilterQuery({ tenantId, channel, status, published, dateFrom, dateTo }),
    [tenantId, channel, status, published, dateFrom, dateTo],
  );
  // S263 F3: the page index belongs to the filters it was chosen under (a filter change starts at page 1 again).
  const [page, setPage] = useState<TracePage>({ filterQuery: "", index: 0 });
  const [pageSize, setPageSize] = useState<number>(TRACE_DEFAULT_PAGE_SIZE);
  const pageIndex = pageFor(page, filterQuery);
  const query = traceQuery(filterQuery, pageIndex, pageSize);

  const q = useQuery({
    queryKey: ["a4-content-log", query],
    queryFn: () => fetchJson<TraceAnswer<ContentLogRow>>(`/api/admin/a4/content-log?${query}`),
    placeholderData: (prev) => prev,  // keep the page on screen while the next one loads
  });
  const answer = q.data ?? null;
  const rows = answer?.data ?? null;
  const loading = q.isPending;
  // The pager follows the page whose rows are on screen (lib/pageRange.ts), not the one being loaded.
  const shown = shownRange(
    answer ? { offset: answer.offset, limit: answer.limit, total: answer.total } : null,
    { page: pageIndex + 1, pageSize },
    rows?.length ?? 0,
  );
  const error = q.isError ? String((q.error as Error).message || q.error) : null;
  const { refetch } = q;
  const load = useCallback(async () => { await refetch(); }, [refetch]);

  // AA-560 — same confirm-dialog + re-fetch-after-success shape as the old a4-oversight page's
  // handleForceUnpublish(). AA-986 L3 (ADR 0015 Q3): the backend changes AA's record only; the post stays live on
  // the tenant's WordPress site / Facebook Page, and once the row is marked the portal no longer offers the tenant
  // its one-click removal (they must delete it on the channel). A live post about a tour AA no longer serves is
  // refused (409 tenant_removes_post): the tenant removes it from the portal. The label, the confirm text and the
  // error line say exactly that.
  const handleForceUnpublish = useCallback(async (publishId: string) => {
    if (!window.confirm(t("confirmUnpublish"))) return;
    setUnpublishingId(publishId);
    setUnpublishError(null);
    try {
      const res = await fetch(`/api/admin/a4/publish-log/${publishId}/unpublish`, { method: "POST" });
      if (!res.ok) {
        const body = await res.json().catch(() => null) as { detail?: unknown } | null;
        const detail = typeof body?.detail === "string"
          ? body.detail.replace(/^tenant_removes_post:\s*/, "") : "";
        throw new Error(detail || `HTTP ${res.status}`);
      }
      await load();
    } catch (e) {
      setUnpublishError(String(e instanceof Error ? e.message : e));
    } finally {
      setUnpublishingId(null);
    }
  }, [load, t]);

  // AA-572 point 2 — tenant stat header: total pieces + per-channel breakdown for the current filters (Tenant
  // included). S263 F3: from the API's own `total` / `by_channel` (every match), not the rows of the loaded page.
  const stats = useMemo(() => traceStats(answer), [answer]);

  return (
    <PageFrame variant="split">
        <PageFrameHeader>
          <PageHeader
            title={t("title")}
            description={t("description")}
          />

          {/* Filter bar — Tenant is the primary lens ("All" is a real, always-valid choice, never
              a placeholder that must be replaced before the table shows anything). */}
          <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", marginTop: 16 }}>
            {/* AA-919 S254: the shared admin tenant picker (active tenants, by name; "" = All tenants). */}
            <TenantSelect inline value={tenantId} onChange={setTenantId} placeholder={t("allTenants")} />
            <FilterField label={t("channel")}>
              <Select value={channel} onChange={e => setChannel(e.target.value)} style={{ ...selectStyle, minWidth: 140 }}>
                {CHANNEL_OPTIONS.map(o => <option key={o.value} value={o.value}>{channelLabel(o.value)}</option>)}
              </Select>
            </FilterField>
            <FilterField label={t("status")}>
              <Select value={status} onChange={e => setStatus(e.target.value)} style={{ ...selectStyle, minWidth: 140 }}>
                {STATUS_OPTIONS.map(o => <option key={o.value} value={o.value}>{t(o.label)}</option>)}
              </Select>
            </FilterField>
            <FilterField label={t("published")}>
              <Select value={published} onChange={e => setPublished(e.target.value)} style={{ ...selectStyle, minWidth: 130 }}>
                {PUBLISHED_OPTIONS.map(o => <option key={o.value} value={o.value}>{t(o.label)}</option>)}
              </Select>
            </FilterField>
            <FilterField label={t("from")}>
              <Input type="date" value={dateFrom} onChange={e => setDateFrom(e.target.value)} style={{ ...selectStyle, cursor: "text" }} />
            </FilterField>
            <FilterField label={t("to")}>
              <Input type="date" value={dateTo} onChange={e => setDateTo(e.target.value)} style={{ ...selectStyle, cursor: "text" }} />
            </FilterField>
          </div>

          {/* AA-572 point 2 — tenant stat header, same tile shape 01-Atomize's own header stat
              bar uses (mirrored inline rather than inventing a differently-shaped one). */}
          {!loading && !error && rows && (
            <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", marginTop: 14 }}>
              <StatTile label={t("totalPieces")} value={stats.total} />
              {stats.byChannel.map(([ch, count]) => (
                <StatTile key={ch} label={channelLabel(ch)} value={count} />
              ))}
            </div>
          )}
        </PageFrameHeader>

        {/* AA-572 point 6 — `overflowX: "auto"` lives HERE (the real, single scrolling ancestor:
            `flex:1, minHeight:0` already correctly bounds its height against the page's own
            `height:"100vh"` chain above) rather than on ContentTraceTable's inner wrapper div —
            see this file's header comment for why that nested overflow-x was silently breaking
            the sticky `<thead>`. AA-575 — the sub-nav + content split below (`a527-dash-body`,
            same class/shape as atom-curation/page.tsx's own body) sets NO overflow of its own, so
            it doesn't reintroduce that bug: this outer div stays the only scrolling ancestor, and
            the table's `<th position:sticky>` still resolves against it unchanged. */}
        <PageFrameBody scroll style={{ overflowX: "auto" }}>
          <SocialContentSplit>
            {/* AA-575 — Content Trace is a genuinely separate Next.js route/page from
                atom-curation/page.tsx (01-05's tab-state owner), which is exactly why it never
                had this sub-nav before: there was no shared layout, just one page's own inline
                JSX. Rendered here with no `onSelectSection` — 01-05 render as real links back to
                `/admin/atom-curation?section=<key>` (see SocialContentSubNav.tsx). */}
            <SocialContentSubNav active="content_trace" />
            <div style={{ flex: 1, minWidth: 0 }}>
              {loading ? (
                <LoadingScreen message={t("loading")} />
              ) : error ? (
                <ErrorState message={error} onRetry={load} />
              ) : !rows || rows.length === 0 ? (
                // AA-601 — "content rendered" signal for the UI smoke: present only once the trace
                // load resolves (never on the LoadingScreen), so a blank/broken page still fails.
                <div data-testid="admin-content-ready">
                  <EmptyState
                    title={t("emptyTitle")}
                    body={t("emptyBody")}
                  />
                </div>
              ) : (
                <div data-testid="admin-content-ready" aria-busy={q.isPlaceholderData || undefined}
                  style={{ opacity: q.isPlaceholderData ? 0.6 : 1, transition: "opacity 120ms" }}>
                  <ContentTraceTable
                    rows={rows}
                    onOpen={(r) => { setUnpublishError(null); setOpenRow(r); }}
                    paging={{
                      pageIndex: shown.page - 1,
                      pageSize: shown.pageSize,
                      total: stats.total,
                      onChange: (index, size) => {
                        if (size !== pageSize) { setPageSize(size); setPage({ filterQuery, index: 0 }); }
                        else setPage({ filterQuery, index });
                      },
                    }}
                  />
                </div>
              )}
            </div>
          </SocialContentSplit>
        </PageFrameBody>
        <Drawer open={openRow != null} onClose={() => { setOpenRow(null); setUnpublishError(null); }} width={760}
          title={openRow?.topic}>
          {/* Force unpublish starts in this Drawer, so its failure shows here (the page behind sits under the scrim). */}
          {openRow && unpublishError && (
            <div role="alert" data-testid="content-trace-unpublish-error"
              style={{ padding: "8px 12px", marginBottom: 12, borderRadius: 6, background: K.dangerTint, color: K.danger, fontSize: 12 }}>
              {unpublishError}
            </div>
          )}
          {openRow && (
            <ContentTraceAccordion
              row={rows?.find(r => r.piece_id === openRow.piece_id) ?? openRow}
              onForceUnpublish={handleForceUnpublish}
              unpublishingId={unpublishingId}
            />
          )}
        </Drawer>
    </PageFrame>
  );
}

function StatTile({ label, value }: { label: string; value: number }) {
  return (
    <div style={{ background: K.card, border: `1px solid ${K.line}`, borderRadius: 8, padding: "6px 12px", minWidth: 92 }}>
      <div style={{ fontSize: 10, color: K.muted, textTransform: "uppercase", letterSpacing: "0.04em" }}>{label}</div>
      <div style={{ fontFamily: mono, fontSize: 15, fontWeight: 600, color: K.ink }}>{value}</div>
    </div>
  );
}

function FilterField({ label, children }: { label: string; children: React.ReactNode }) {
  const t = useT(TRACE);
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
      <span style={{ fontSize: 11.5, color: K.muted }}>{t("filterLabel", label)}</span>
      {children}
    </div>
  );
}

// ══════════════════════════════════════════════════════════════════════════
// Main table — 1 row = 1 write attempt, click to expand the accordion below it (SlateTab.tsx /
// AA-564 SubjectRow pattern: one shared `expandedId` at the table level, at most one row open).
// ══════════════════════════════════════════════════════════════════════════

// AA-572 shipped only 3 sortable columns (Gate/Retry/Created at); AA-575 extends every column to the same mechanism.
// AA-1006: the kit DataTable sorts by `traceSortValue` (rows of the page on screen). S263 F3: seven columns with
// readable widths, so the table fits its pane at 1440 (see the file header).
type TracePaging = { pageIndex: number; pageSize: number; total: number; onChange: (pageIndex: number, pageSize: number) => void };

/** A cell's text box: wraps between `min` and `max` px (a two-word Topic or Angle no longer squeezes to one word a line). */
function textBox(min: number, max: number): React.CSSProperties {
  return { display: "block", minWidth: min, maxWidth: max, whiteSpace: "normal", overflowWrap: "anywhere" };
}

function ContentTraceTable({ rows, onOpen, paging }: {
  rows: ContentLogRow[]; onOpen: (row: ContentLogRow) => void; paging: TracePaging;
}) {
  const t = useT(TRACE);
  const { formatDateTime } = useFormat();
  const columns = useMemo<ColumnDef<ContentLogRow, unknown>[]>(() => {
    const col = (key: TraceSortKey, label: string, cell: (r: ContentLogRow) => React.ReactNode): ColumnDef<ContentLogRow, unknown> => ({
      id: key, header: label, accessorFn: (r) => traceSortValue(r, key, (c) => CHANNEL_LABEL[c] ?? c), sortDescFirst: true,
      cell: ({ row }) => cell(row.original),
    });
    const sub: React.CSSProperties = { display: "block", marginTop: 3, fontSize: 11.5, color: K.muted };
    return [
      col("topic", t("colTopic"), (r) => <span style={textBox(150, 220)}>{r.topic}</span>),
      col("tenant", t("colTenantTour"), (r) => (
        <span style={textBox(110, 170)}>
          <span title={r.tenant_id} style={{ color: K.ink }}>{r.tenant_name ?? "—"}</span>
          <span style={sub}>{r.tour?.name ?? "—"}</span>
        </span>
      )),
      col("channel", t("channel"), (r) => <Badge tone="neutral">{r.channel}</Badge>),
      col("angle", t("colAngle"), (r) => <span style={textBox(140, 200)}>{r.angles.find(a => a.chosen)?.name ?? "—"}</span>),
      col("status", t("status"), (r) => (
        <span style={{ display: "block", minWidth: 0 }}>
          <Badge tone={STATUS_COLOR[r.status] ?? "neutral"}>{r.status}</Badge>
          {r.publish_status === "published" ? (
            r.publish_external_url ? (
              <a href={r.publish_external_url} target="_blank" rel="noreferrer" onClick={e => e.stopPropagation()}
                style={{ ...sub, color: K.accent }}>{t("publishedLink")}</a>
            ) : <span style={sub}>{t("published")}</span>
          ) : r.publish_status === "pending_publish" ? (
            <span style={sub}>{t("notPublishedYet")}</span>
          ) : null}
        </span>
      )),
      col("gate", t("colGates"), (r) => (
        <span style={{ display: "block", minWidth: 0 }}>
          <span style={{ fontFamily: mono }}>{r.gate_pass_count}/{r.gate_total_count}</span>
          <span style={sub}>
            {t("retries", r.retry_count)}
            {r.is_buffer_retry && <span title={t("bufferTitle")}>{t("buffer")}</span>}
          </span>
        </span>
      )),
      col("created_at", t("colCreated"), (r) => (
        // "1 Nov 2026, 00:50" breaks after the comma: the date over the time.
        <span style={{ ...textBox(72, 96), overflowWrap: "normal" }}>{formatDateTime(r.created_at)}</span>
      )),
    ];
  }, [t, formatDateTime]);
  return (
    <DataTable<ContentLogRow>
      tableId="a4-content-trace"
      data={rows}
      columns={columns}
      getRowId={(r) => r.piece_id}
      onRowClick={onOpen}
      searchable={false}
      serverPagination={paging}
      pageSize={TRACE_DEFAULT_PAGE_SIZE}
      pageSizeOptions={[...TRACE_PAGE_SIZES]}
      compact
    />
  );
}

// ══════════════════════════════════════════════════════════════════════════
// Accordion — full lineage + all 3 angles (chosen marked) + content + gate detail + retry
// reasoning ("Retry reason", never a content diff — see file header) + publish status.
// ══════════════════════════════════════════════════════════════════════════

function ContentTraceAccordion({ row: p, onForceUnpublish, unpublishingId }: {
  row: ContentLogRow; onForceUnpublish: (publishId: string) => void; unpublishingId: string | null;
}) {
  const t = useT(TRACE);
  const { formatDateTime } = useFormat();
  return (
    <Section style={{ marginTop: 4 }} bodyStyle={{ padding: "16px 18px" }}>
      {/* AA-572 point 5 — Lineage: was 1 concatenated line ("Tour: X → Atom: Y → Slate: Segment —
          Z"), hard to scan. Now one line per real entity (Tour/Atom/Segment/Route/Hub/Slate) —
          always visible, never a blank cell (the pre-Slate direct-atom path is a real,
          explicitly-labeled state, not an omission). Route/Hub were previously merged into one
          `sourceLabel()` string ("Route/Hub — {hub} (Day X-Y)") — now genuinely 2 lines, matching
          the issue's own entity list. */}
      <SectionLabel>{t("lineage")}</SectionLabel>
      <div style={{ marginBottom: 4 }}>
        <LineageLine label={t("tour")}>
          {p.tour?.name ?? "—"}{p.tour?.destination ? ` (${p.tour.destination})` : ""}
        </LineageLine>
        {p.atom && <LineageLine label={t("atom")}>{p.atom.text}</LineageLine>}
        {p.source.kind === "segment" && (
          <LineageLine label={t("segment")}>
            {p.source.place ?? "?"}{p.source.action ? ` (${p.source.action})` : ""}
          </LineageLine>
        )}
        {p.source.kind === "route" && (
          <>
            <LineageLine label={t("route")}>{t("days", String(p.source.first_day), String(p.source.last_day))}</LineageLine>
            <LineageLine label={t("hub")}>{p.source.hub_name ?? "?"}</LineageLine>
          </>
        )}
        <LineageLine label={t("slate")}>
          {p.source.kind === "direct_atom" ? t("slateDirect") : t("slatePicked")}
        </LineageLine>
        {p.job_id && (
          <LineageLine label={t("job")}>
            <a href={`/admin/jobs?job=${p.job_id}`} style={{ color: K.accent, fontFamily: mono }}>
              {t("jobLink", p.job_id.slice(0, 8))}
            </a>
          </LineageLine>
        )}
      </div>
      {p.goal && <div style={{ fontSize: 12.5, color: K.body, marginBottom: 12 }}><strong>{t("goal")}</strong> {p.goal}</div>}
      {p.cta && <div style={{ fontSize: 12.5, color: K.body, marginBottom: 12 }}><strong>{t("cta")}</strong> {p.cta}</div>}

      {/* All 3 angles, chosen one marked — never just the pick. */}
      <SectionLabel>{t("anglesTitle", p.angles.length)}</SectionLabel>
      {p.angles.length === 0 ? (
        <div style={{ fontSize: 12, color: K.muted2, marginBottom: 14 }}>{t("noAngles")}</div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 6, marginBottom: 14 }}>
          {p.angles.map(a => (
            <div key={a.option_id} style={{
              border: `1px solid ${a.chosen ? K.accent : K.line}`, borderRadius: 8, padding: "8px 10px",
              background: a.chosen ? K.accentTint : "transparent",
            }}>
              <div style={{ fontSize: 12.5, fontWeight: 600, color: K.body, marginBottom: 2, display: "flex", alignItems: "center", gap: 6 }}>
                {a.name}
                {a.chosen && <Badge tone="accent">{t("chosen")}</Badge>}
                {a.recommended && !a.chosen && <Badge tone="neutral">{t("recommended")}</Badge>}
              </div>
              <div style={{ fontSize: 11.5, color: K.muted }}>{a.why_it_works}</div>
            </div>
          ))}
        </div>
      )}

      {/* Full content. AA-572 point 4 — no `maxHeight`/`overflowY` scrollbox anymore: the whole
          piece renders inline so it can be read/scanned without cramming into a small nested
          scroll area (the accordion itself already scrolls within the page). */}
      <SectionLabel>{t("content")}</SectionLabel>
      <div style={{
        fontSize: 12.5, color: K.body, lineHeight: 1.6, marginBottom: 14, whiteSpace: "pre-wrap",
        border: `1px solid ${K.line}`, borderRadius: 8, padding: "10px 12px",
        background: K.card,
      }}>
        {p.content_text || t("noContent")}
      </div>
      {p.held_reason && (
        <div style={{ fontSize: 12, color: K.danger, marginBottom: 14 }}><strong>{t("heldReason")}</strong> {p.held_reason}</div>
      )}

      {/* Gate ledger — specific violation text, not just counts. */}
      <SectionLabel>{t("gateLedger", p.gate_pass_count, p.gate_total_count)}</SectionLabel>
      {p.gate_ledger.length === 0 ? (
        <div style={{ fontSize: 12, color: K.muted2, marginBottom: 14 }}>{t("noGates")}</div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 6, marginBottom: 14 }}>
          {p.gate_ledger.map((g, i) => (
            <div key={i} style={{ fontSize: 12, color: K.body }}>
              <Badge tone={g.passed ? "success" : "danger"}>{g.gate ?? "gate"}</Badge>
              {!g.passed && g.violations && g.violations.length > 0 && (
                <ul style={{ margin: "4px 0 0 20px", padding: 0, color: K.muted }}>
                  {g.violations.map((v, j) => <li key={j} style={{ fontSize: 11.5 }}>{v}</li>)}
                </ul>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Retry — real reason, labeled honestly. Never "content changed" — that data was never
          persisted (STEP0, confirmed structural, AA-570 is the separate follow-up). */}
      <SectionLabel>{t("retryHistory", p.retry_count)}</SectionLabel>
      {p.repair_log.length === 0 ? (
        <div style={{ fontSize: 12, color: K.muted2, marginBottom: 4 }}>
          {p.status === "approved" || p.status === "held" ? t("noRetriesFinal") : t("noRetries")}
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 8, marginBottom: 4 }}>
          {p.repair_log.map((round, i) => (
            <div key={i} style={{ fontSize: 12, color: K.body, border: `1px solid ${K.line}`, borderRadius: 8, padding: "8px 10px" }}>
              <div style={{ fontWeight: 600, marginBottom: 3 }}>
                {t("round", round.attempt ?? i + 1, round.gate_targeted ?? "")}
              </div>
              {round.violations && round.violations.length > 0 ? (
                <ul style={{ margin: 0, paddingLeft: 18, color: K.muted }}>
                  {round.violations.map((v, j) => <li key={j} style={{ fontSize: 11.5 }}>{v}</li>)}
                </ul>
              ) : (
                <div style={{ fontSize: 11.5, color: K.muted2 }}>{t("noViolation")}</div>
              )}
            </div>
          ))}
        </div>
      )}
      {p.is_buffer_retry && (
        <div style={{ fontSize: 11.5, color: K.muted, marginTop: 6 }}>
          {t("bufferNote", p.sibling_piece_count)}
        </div>
      )}

      {/* Publish status. */}
      <SectionLabel style={{ marginTop: 14 }}>{t("publish")}</SectionLabel>
      <div style={{ fontSize: 12.5, color: K.body, display: "flex", alignItems: "center", gap: 8 }}>
        <Badge tone={p.publish_status === "published" ? "success" : p.publish_status === "pending_publish" ? "warning" : "neutral"}>
          {p.publish_status}
        </Badge>
        {p.publish_external_url && (
          <a href={p.publish_external_url} target="_blank" rel="noreferrer" style={{ color: K.accent }}>{t("viewPost")}</a>
        )}
        {p.publish_published_at && (
          <span style={{ fontSize: 11.5, color: K.muted2 }}>{t("publishedAt", formatDateTime(p.publish_published_at))}</span>
        )}
        {/* AA-560 — moved here from the deleted a4-oversight page's own Publish Log section, same
            confirm-then-call shape. Only a published row with a real publish_id can be
            unpublished — a pending/n/a row has no publish_log row to act on. */}
        {p.publish_status === "published" && p.publish_id && (
          // AA-986 L3: the record only, never the channel (see handleForceUnpublish)
          <Button size="sm" variant="danger"
            onClick={() => onForceUnpublish(p.publish_id as string)}
            disabled={unpublishingId === p.publish_id}
          >
            {unpublishingId === p.publish_id ? t("marking") : t("markUnpublished")}
          </Button>
        )}
      </div>
    </Section>
  );
}

// AA-572 point 5 — one Lineage entity per line (label + value), replacing the old single
// concatenated string.
function LineageLine({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ fontSize: 12.5, color: K.body, lineHeight: 1.7 }}>
      <strong>{label}:</strong> {children}
    </div>
  );
}

function SectionLabel({ children, style }: { children: React.ReactNode; style?: React.CSSProperties }) {
  return (
    <div style={{
      fontSize: 11, fontWeight: 600, color: K.ink3, textTransform: "uppercase",
      letterSpacing: "0.06em", marginBottom: 6, display: "flex", alignItems: "center", gap: 6, ...style,
    }}>
      <Radio size={11} style={{ opacity: 0.5 }} />{children}
    </div>
  );
}
