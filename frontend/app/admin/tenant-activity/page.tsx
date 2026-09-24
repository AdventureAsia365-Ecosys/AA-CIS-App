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
// middleware.ts PROTECTED_ROUTES already allowlists `/admin/tenant-activity`), genuinely a peer
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
import { Fragment, useState, useEffect, useCallback, useMemo } from "react";
import { ChevronDown, ChevronRight, ChevronUp, Radio } from "lucide-react";
import AdminSidebar from "../_components/AdminSidebar";
import SocialContentSubNav from "../_components/SocialContentSubNav";
import { A, serif, mono, sans, Card, Badge, LoadingScreen } from "../_components/adminUi";
import { fetchJson, EmptyState, ErrorState } from "../_components/auditPanels";

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
  created_at: string;
}

interface Tenant { tenant_id: string; name: string; slug: string; }

const STATUS_COLOR: Record<string, "green" | "amber" | "red" | "gray"> = {
  approved: "green", held: "amber", processing: "gray", failed: "red",
};

const STATUS_OPTIONS = [
  { value: "", label: "All statuses" },
  { value: "processing", label: "Processing" },
  { value: "approved", label: "Approved" },
  { value: "held", label: "Held" },
  { value: "failed", label: "Failed" },
];

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
  { value: "", label: "Any" },
  { value: "yes", label: "Published" },
  { value: "no", label: "Not published" },
];

const selectStyle: React.CSSProperties = {
  padding: "8px 12px", background: A.card, border: `1px solid ${A.line}`, borderRadius: 8,
  fontSize: 12.5, fontFamily: sans, color: A.body, cursor: "pointer",
};

// AA-572 point 3 — sort state lives at the page level (not the table's) so it can be reset
// alongside the filters if a future build wants that; for now it just persists across re-fetches.
// AA-575 — extended from the original 3 (gate/retry/created_at) to every column, per Nghiệp's
// explicit "TẤT CẢ cột" follow-up request; same mechanism, no per-column special-casing beyond
// `sortValue()` below picking the right field/rank.
type SortKey =
  | "topic" | "tenant" | "tour" | "channel" | "angle" | "status"
  | "gate" | "retry" | "published" | "created_at";
type Sort = { key: SortKey; dir: "asc" | "desc" };

const CHANNEL_LABEL: Record<string, string> = Object.fromEntries(
  CHANNEL_OPTIONS.filter(o => o.value).map(o => [o.value, o.label]),
);

// AA-575 — Published has no single obvious sort field (it's a 3-state enum, not free text or a
// number): ranked in pipeline order (not published → pending → published) rather than
// alphabetically, so ascending/descending reads as "progress toward live" instead of a meaningless
// string sort ("n/a" vs "pending_publish" vs "published" alphabetically doesn't match reality).
const PUBLISH_RANK: Record<ContentLogRow["publish_status"], number> = {
  "n/a": 0, "pending_publish": 1, "published": 2,
};

function sortValue(r: ContentLogRow, key: SortKey): string | number {
  switch (key) {
    case "topic": return r.topic ?? "";
    case "tenant": return r.tenant_name ?? "";
    case "tour": return r.tour?.name ?? "";
    case "channel": return CHANNEL_LABEL[r.channel] ?? r.channel;
    case "angle": return r.angles.find(a => a.chosen)?.name ?? "";
    case "status": return r.status ?? "";
    case "gate": return r.gate_pass_count;
    case "retry": return r.retry_count;
    case "published": return PUBLISH_RANK[r.publish_status];
    case "created_at": return new Date(r.created_at).getTime();
  }
}

export default function ContentTracePage() {
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [tenantId, setTenantId] = useState("");
  const [channel, setChannel] = useState("");
  const [status, setStatus] = useState("");
  const [published, setPublished] = useState("");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");

  const [rows, setRows] = useState<ContentLogRow[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [sort, setSort] = useState<Sort | null>(null);
  // AA-560 — Force unpublish state, same shape a4-oversight/page.tsx's own PublishLogSection used.
  const [unpublishingId, setUnpublishingId] = useState<string | null>(null);
  const [unpublishError, setUnpublishError] = useState<string | null>(null);

  useEffect(() => {
    fetchJson<{ tenants: Tenant[] }>("/api/admin/tenants")
      .then(d => setTenants(d.tenants))
      .catch(() => {});
  }, []);

  const query = useMemo(() => {
    const p = new URLSearchParams();
    if (tenantId) p.set("tenant_id", tenantId);
    if (channel) p.set("channel", channel);
    if (status) p.set("status", status);
    if (published) p.set("published", published);
    if (dateFrom) p.set("date_from", dateFrom);
    if (dateTo) p.set("date_to", dateTo);
    return p.toString();
  }, [tenantId, channel, status, published, dateFrom, dateTo]);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    fetchJson<{ data: ContentLogRow[]; total: number }>(`/api/admin/a4/content-log?${query}`)
      .then(d => setRows(d.data))
      .catch(e => setError(String(e.message || e)))
      .finally(() => setLoading(false));
  }, [query]);

  useEffect(() => { load(); }, [load]);

  // AA-560 — same confirm-dialog + re-fetch-after-success shape as the old a4-oversight page's
  // handleForceUnpublish(). Destructive/irreversible from here (real, immediate publish-status
  // change on the actual channel), hence the native confirm().
  const handleForceUnpublish = useCallback(async (publishId: string) => {
    if (!window.confirm("Force-unpublish this piece? This cannot be undone from here.")) return;
    setUnpublishingId(publishId);
    setUnpublishError(null);
    try {
      const res = await fetch(`/api/admin/a4/publish-log/${publishId}/unpublish`, { method: "POST" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      await load();
    } catch (e) {
      setUnpublishError(String(e instanceof Error ? e.message : e));
    } finally {
      setUnpublishingId(null);
    }
  }, [load]);

  // AA-572 point 2 — tenant stat header: total pieces + per-channel breakdown, computed
  // client-side from the already-loaded (already tenant/channel/status/date-filtered) `rows` —
  // no new endpoint, matches the issue's own "no backend change" scope. Reflects whatever the
  // current filters (Tenant included) narrow the table down to, exactly like Social Content's
  // 01-Atomize header stat bar already does for its own Tour/Market filters.
  const stats = useMemo(() => {
    const list = rows ?? [];
    const byChannel: Record<string, number> = {};
    for (const r of list) byChannel[r.channel] = (byChannel[r.channel] ?? 0) + 1;
    return {
      total: list.length,
      byChannel: Object.entries(byChannel).sort((a, b) => b[1] - a[1]),
    };
  }, [rows]);

  // AA-572 point 3 / AA-575 — sort applied client-side to the already-loaded rows (same "no
  // backend change" scope as the stat header above); the endpoint has no `sort` param and
  // doesn't need one for a page-sized result set (`limit` caps at 500). `sortValue()` returns
  // either a string (free-text/categorical columns) or a number (counts, the Published rank,
  // and the Created-at timestamp) — compared with `localeCompare` or subtraction accordingly.
  const sortedRows = useMemo(() => {
    if (!rows || !sort) return rows;
    const withKey = rows.map(r => ({ r, k: sortValue(r, sort.key) }));
    withKey.sort((a, b) => {
      const cmp = typeof a.k === "string" && typeof b.k === "string"
        ? a.k.localeCompare(b.k)
        : (a.k as number) - (b.k as number);
      return sort.dir === "asc" ? cmp : -cmp;
    });
    return withKey.map(x => x.r);
  }, [rows, sort]);

  const toggleSort = useCallback((key: SortKey) => {
    setSort(prev => prev?.key === key ? { key, dir: prev.dir === "desc" ? "asc" : "desc" } : { key, dir: "desc" });
  }, []);

  return (
    <div style={{ display: "flex", minHeight: "100vh", background: A.bg, fontFamily: sans }}>
      <AdminSidebar />
      <div style={{ flex: 1, display: "flex", flexDirection: "column", height: "100vh" }}>
        <div style={{ flexShrink: 0, background: A.bg, padding: "28px 32px 16px", borderBottom: `1px solid ${A.line}` }}>
          <h1 style={{ fontFamily: serif, fontSize: 26, fontWeight: 500, color: A.ink, margin: 0 }}>
            06 · Content Trace
          </h1>
          <div style={{ fontSize: 12, color: A.muted, marginTop: 4, maxWidth: 760 }}>
            Every content piece written across ALL tenants, one row per write attempt — full
            lineage, all 3 generated angles, gate detail, and retry reasoning on click. Admin-only
            lesson log, not the tenant&apos;s own view of their content.
          </div>

          {/* Filter bar — Tenant is the primary lens ("All" is a real, always-valid choice, never
              a placeholder that must be replaced before the table shows anything). */}
          <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", marginTop: 16 }}>
            <FilterField label="Tenant">
              <select value={tenantId} onChange={e => setTenantId(e.target.value)} style={{ ...selectStyle, minWidth: 180, fontWeight: 600 }}>
                <option value="">All tenants</option>
                {tenants.map(t => <option key={t.tenant_id} value={t.tenant_id}>{t.name}</option>)}
              </select>
            </FilterField>
            <FilterField label="Channel">
              <select value={channel} onChange={e => setChannel(e.target.value)} style={{ ...selectStyle, minWidth: 140 }}>
                {CHANNEL_OPTIONS.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
              </select>
            </FilterField>
            <FilterField label="Status">
              <select value={status} onChange={e => setStatus(e.target.value)} style={{ ...selectStyle, minWidth: 140 }}>
                {STATUS_OPTIONS.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
              </select>
            </FilterField>
            <FilterField label="Published">
              <select value={published} onChange={e => setPublished(e.target.value)} style={{ ...selectStyle, minWidth: 130 }}>
                {PUBLISHED_OPTIONS.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
              </select>
            </FilterField>
            <FilterField label="From">
              <input type="date" value={dateFrom} onChange={e => setDateFrom(e.target.value)} style={{ ...selectStyle, cursor: "text" }} />
            </FilterField>
            <FilterField label="To">
              <input type="date" value={dateTo} onChange={e => setDateTo(e.target.value)} style={{ ...selectStyle, cursor: "text" }} />
            </FilterField>
          </div>

          {/* AA-572 point 2 — tenant stat header, same tile shape 01-Atomize's own header stat
              bar uses (adminUi.tsx has no shared StatTile export yet, so this mirrors it inline
              rather than inventing a differently-shaped one). */}
          {!loading && !error && rows && (
            <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", marginTop: 14 }}>
              <StatTile label="Total pieces" value={stats.total} />
              {stats.byChannel.map(([ch, count]) => (
                <StatTile key={ch} label={CHANNEL_LABEL[ch] ?? ch} value={count} />
              ))}
            </div>
          )}
        </div>

        {/* AA-572 point 6 — `overflowX: "auto"` lives HERE (the real, single scrolling ancestor:
            `flex:1, minHeight:0` already correctly bounds its height against the page's own
            `height:"100vh"` chain above) rather than on ContentTraceTable's inner wrapper div —
            see this file's header comment for why that nested overflow-x was silently breaking
            the sticky `<thead>`. AA-575 — the sub-nav + content split below (`a527-dash-body`,
            same class/shape as atom-curation/page.tsx's own body) sets NO overflow of its own, so
            it doesn't reintroduce that bug: this outer div stays the only scrolling ancestor, and
            the table's `<th position:sticky>` still resolves against it unchanged. */}
        <div style={{ flex: 1, minHeight: 0, overflowY: "auto", overflowX: "auto", padding: "20px 32px 32px" }}>
          <div className="a527-dash-body" style={{ display: "flex", gap: 20, alignItems: "flex-start" }}>
            {/* AA-575 — Content Trace is a genuinely separate Next.js route/page from
                atom-curation/page.tsx (01-05's tab-state owner), which is exactly why it never
                had this sub-nav before: there was no shared layout, just one page's own inline
                JSX. Rendered here with no `onSelectSection` — 01-05 render as real links back to
                `/admin/atom-curation?section=<key>` (see SocialContentSubNav.tsx). */}
            <SocialContentSubNav active="content_trace" />
            <div style={{ flex: 1, minWidth: 0 }}>
              {unpublishError && (
                <div style={{ padding: "8px 12px", marginBottom: 12, borderRadius: 6, background: A.redTint, color: A.red, fontSize: 12 }}>
                  {unpublishError}
                </div>
              )}
              {loading ? (
                <LoadingScreen msg="Loading Content Trace…" />
              ) : error ? (
                <ErrorState message={error} onRetry={load} />
              ) : !rows || rows.length === 0 ? (
                <EmptyState
                  title="No content pieces match these filters"
                  body="Nothing has been written yet for this combination of tenant/channel/status/date — try widening a filter, or 'All tenants' to check whether the data exists elsewhere."
                />
              ) : (
                <ContentTraceTable rows={sortedRows ?? rows} expandedId={expandedId} sort={sort} onSort={toggleSort}
                  onToggle={id => setExpandedId(prev => prev === id ? null : id)}
                  onForceUnpublish={handleForceUnpublish} unpublishingId={unpublishingId} />
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function StatTile({ label, value }: { label: string; value: number }) {
  return (
    <div style={{ background: A.card, border: `1px solid ${A.line}`, borderRadius: 8, padding: "6px 12px", minWidth: 92 }}>
      <div style={{ fontSize: 10, color: A.muted, textTransform: "uppercase", letterSpacing: "0.04em" }}>{label}</div>
      <div style={{ fontFamily: mono, fontSize: 15, fontWeight: 600, color: A.ink }}>{value}</div>
    </div>
  );
}

function FilterField({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
      <span style={{ fontSize: 11.5, color: A.muted }}>{label}:</span>
      {children}
    </div>
  );
}

// ══════════════════════════════════════════════════════════════════════════
// Main table — 1 row = 1 write attempt, click to expand the accordion below it (SlateTab.tsx /
// AA-564 SubjectRow pattern: one shared `expandedId` at the table level, at most one row open).
// ══════════════════════════════════════════════════════════════════════════

const COLS = 10;

// AA-572 shipped only 3 sortable columns (Gate/Retry/Created at); AA-575 extends every remaining
// column (Topic/Tenant/Tour/Channel/Angle chosen/Status/Published) to the same mechanism, per
// Nghiệp's explicit follow-up — only the leading expand-chevron column has no `sortKey`.
const HEADERS: { label: string; sortKey?: SortKey }[] = [
  { label: "" },
  { label: "Topic", sortKey: "topic" },
  { label: "Tenant", sortKey: "tenant" },
  { label: "Tour", sortKey: "tour" },
  { label: "Channel", sortKey: "channel" },
  { label: "Angle chosen", sortKey: "angle" },
  { label: "Status", sortKey: "status" },
  { label: "Gate count", sortKey: "gate" },
  { label: "Retry count", sortKey: "retry" },
  { label: "Published", sortKey: "published" },
  { label: "Created at", sortKey: "created_at" },
];

function ContentTraceTable({ rows, expandedId, sort, onSort, onToggle, onForceUnpublish, unpublishingId }: {
  rows: ContentLogRow[]; expandedId: string | null; sort: Sort | null; onSort: (key: SortKey) => void;
  onToggle: (id: string) => void;
  onForceUnpublish: (publishId: string) => void; unpublishingId: string | null;
}) {
  return (
    // AA-572 point 6 — deliberately NO `overflowX` here (default `visible`): the outer page
    // scroll container now owns horizontal scroll too, so this div isn't its own overflow
    // context and doesn't hijack `<th>`'s `position: sticky` — see file header comment.
    <div style={{ border: `1px solid ${A.line}`, borderRadius: 10 }}>
      <table style={{ width: "100%", borderCollapse: "collapse", fontFamily: sans }}>
        <thead>
          <tr>
            {HEADERS.map((h, i) => {
              const active = h.sortKey && sort?.key === h.sortKey;
              return (
                <th key={i} onClick={h.sortKey ? () => onSort(h.sortKey as SortKey) : undefined} style={{
                  padding: "10px 14px", fontSize: 11, fontWeight: 600, textTransform: "uppercase",
                  letterSpacing: "0.08em", color: active ? A.gold : A.muted, textAlign: "left",
                  background: A.bg, borderBottom: `1px solid ${A.line}`, whiteSpace: "nowrap",
                  position: "sticky", top: 0, zIndex: 2, cursor: h.sortKey ? "pointer" : "default",
                  userSelect: "none",
                }}>
                  <span style={{ display: "inline-flex", alignItems: "center", gap: 3 }}>
                    {h.label}
                    {h.sortKey && (
                      active ? (
                        sort!.dir === "asc" ? <ChevronUp size={12} /> : <ChevronDown size={12} />
                      ) : <ChevronDown size={12} style={{ opacity: 0.25 }} />
                    )}
                  </span>
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {rows.map(r => {
            const expanded = expandedId === r.piece_id;
            const chosenAngle = r.angles.find(a => a.chosen);
            return (
              <Fragment key={r.piece_id}>
                <tr onClick={() => onToggle(r.piece_id)} style={{
                  cursor: "pointer", background: expanded ? A.goldTint : "transparent",
                }}>
                  <td style={rowTd}>{expanded ? <ChevronDown size={14} color={A.gold} /> : <ChevronRight size={14} color={A.muted2} />}</td>
                  <td style={{ ...rowTd, maxWidth: 260, whiteSpace: "normal" }}>{r.topic}</td>
                  <td style={rowTd}>{r.tenant_name ?? "—"}</td>
                  <td style={rowTd}>{r.tour?.name ?? "—"}</td>
                  <td style={rowTd}><Badge color="gray">{r.channel}</Badge></td>
                  <td style={{ ...rowTd, maxWidth: 200, whiteSpace: "normal" }}>{chosenAngle?.name ?? "—"}</td>
                  <td style={rowTd}><Badge color={STATUS_COLOR[r.status] ?? "gray"}>{r.status}</Badge></td>
                  <td style={{ ...rowTd, fontFamily: mono }}>{r.gate_pass_count}/{r.gate_total_count}</td>
                  <td style={{ ...rowTd, fontFamily: mono }}>
                    {r.retry_count}{r.is_buffer_retry && <span title="Also a buffer-retry of an earlier held piece"> +buffer</span>}
                  </td>
                  <td style={rowTd}>
                    {r.publish_status === "published" ? (
                      r.publish_external_url ? (
                        <a href={r.publish_external_url} target="_blank" rel="noreferrer"
                          onClick={e => e.stopPropagation()} style={{ color: A.gold }}>Yes ↗</a>
                      ) : <Badge color="green">Yes</Badge>
                    ) : r.publish_status === "pending_publish" ? (
                      <Badge color="amber">Not yet</Badge>
                    ) : <Badge color="gray">No</Badge>}
                  </td>
                  <td style={rowTd}>{new Date(r.created_at).toLocaleString()}</td>
                </tr>
                {expanded && (
                  <tr>
                    <td colSpan={COLS} style={{ padding: "0 14px 18px", background: A.bg, borderBottom: `1px solid ${A.line}` }}>
                      <ContentTraceAccordion row={r} onForceUnpublish={onForceUnpublish} unpublishingId={unpublishingId} />
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

const rowTd: React.CSSProperties = {
  padding: "11px 14px", fontSize: 12.5, color: A.body, borderBottom: `1px solid ${A.line2}`,
  whiteSpace: "nowrap",
};

// ══════════════════════════════════════════════════════════════════════════
// Accordion — full lineage + all 3 angles (chosen marked) + content + gate detail + retry
// reasoning ("Retry reason", never a content diff — see file header) + publish status.
// ══════════════════════════════════════════════════════════════════════════

function ContentTraceAccordion({ row: p, onForceUnpublish, unpublishingId }: {
  row: ContentLogRow; onForceUnpublish: (publishId: string) => void; unpublishingId: string | null;
}) {
  return (
    <Card style={{ padding: "16px 18px", marginTop: 4 }}>
      {/* AA-572 point 5 — Lineage: was 1 concatenated line ("Tour: X → Atom: Y → Slate: Segment —
          Z"), hard to scan. Now one line per real entity (Tour/Atom/Segment/Route/Hub/Slate) —
          always visible, never a blank cell (the pre-Slate direct-atom path is a real,
          explicitly-labeled state, not an omission). Route/Hub were previously merged into one
          `sourceLabel()` string ("Route/Hub — {hub} (Day X-Y)") — now genuinely 2 lines, matching
          the issue's own entity list. */}
      <SectionLabel>Lineage</SectionLabel>
      <div style={{ marginBottom: 4 }}>
        <LineageLine label="Tour">
          {p.tour?.name ?? "—"}{p.tour?.destination ? ` (${p.tour.destination})` : ""}
        </LineageLine>
        {p.atom && <LineageLine label="Atom">{p.atom.text}</LineageLine>}
        {p.source.kind === "segment" && (
          <LineageLine label="Segment">
            {p.source.place ?? "?"}{p.source.action ? ` (${p.source.action})` : ""}
          </LineageLine>
        )}
        {p.source.kind === "route" && (
          <>
            <LineageLine label="Route">Day {p.source.first_day}–{p.source.last_day}</LineageLine>
            <LineageLine label="Hub">{p.source.hub_name ?? "?"}</LineageLine>
          </>
        )}
        <LineageLine label="Slate">
          {p.source.kind === "direct_atom" ? "Not used — atom chosen directly" : "Picked from the Slate"}
        </LineageLine>
      </div>
      {p.goal && <div style={{ fontSize: 12.5, color: A.body, marginBottom: 12 }}><strong>Goal:</strong> {p.goal}</div>}
      {p.cta && <div style={{ fontSize: 12.5, color: A.body, marginBottom: 12 }}><strong>CTA:</strong> {p.cta}</div>}

      {/* All 3 angles, chosen one marked — never just the pick. */}
      <SectionLabel>Angles generated ({p.angles.length}) — chosen one marked</SectionLabel>
      {p.angles.length === 0 ? (
        <div style={{ fontSize: 12, color: A.muted2, marginBottom: 14 }}>No angle options on record for this request.</div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 6, marginBottom: 14 }}>
          {p.angles.map(a => (
            <div key={a.option_id} style={{
              border: `1px solid ${a.chosen ? A.gold : A.line}`, borderRadius: 8, padding: "8px 10px",
              background: a.chosen ? A.goldTint : "transparent",
            }}>
              <div style={{ fontSize: 12.5, fontWeight: 600, color: A.body, marginBottom: 2, display: "flex", alignItems: "center", gap: 6 }}>
                {a.name}
                {a.chosen && <Badge color="gold">chosen</Badge>}
                {a.recommended && !a.chosen && <Badge color="gray">recommended</Badge>}
              </div>
              <div style={{ fontSize: 11.5, color: A.muted }}>{a.why_it_works}</div>
            </div>
          ))}
        </div>
      )}

      {/* Full content. AA-572 point 4 — no `maxHeight`/`overflowY` scrollbox anymore: the whole
          piece renders inline so it can be read/scanned without cramming into a small nested
          scroll area (the accordion itself already scrolls within the page). */}
      <SectionLabel>Content</SectionLabel>
      <div style={{
        fontSize: 12.5, color: A.body, lineHeight: 1.6, marginBottom: 14, whiteSpace: "pre-wrap",
        border: `1px solid ${A.line}`, borderRadius: 8, padding: "10px 12px",
        background: A.card,
      }}>
        {p.content_text || "(no content text on record)"}
      </div>
      {p.held_reason && (
        <div style={{ fontSize: 12, color: A.red, marginBottom: 14 }}><strong>Held reason:</strong> {p.held_reason}</div>
      )}

      {/* Gate ledger — specific violation text, not just counts. */}
      <SectionLabel>Gate ledger ({p.gate_pass_count}/{p.gate_total_count} passed)</SectionLabel>
      {p.gate_ledger.length === 0 ? (
        <div style={{ fontSize: 12, color: A.muted2, marginBottom: 14 }}>No gate results on record.</div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 6, marginBottom: 14 }}>
          {p.gate_ledger.map((g, i) => (
            <div key={i} style={{ fontSize: 12, color: A.body }}>
              <Badge color={g.passed ? "green" : "red"}>{g.gate ?? "gate"}</Badge>
              {!g.passed && g.violations && g.violations.length > 0 && (
                <ul style={{ margin: "4px 0 0 20px", padding: 0, color: A.muted }}>
                  {g.violations.map((v, j) => <li key={j} style={{ fontSize: 11.5 }}>{v}</li>)}
                </ul>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Retry — real reason, labeled honestly. Never "content changed" — that data was never
          persisted (STEP0, confirmed structural, AA-570 is the separate follow-up). */}
      <SectionLabel>Retry history ({p.retry_count})</SectionLabel>
      {p.repair_log.length === 0 ? (
        <div style={{ fontSize: 12, color: A.muted2, marginBottom: 4 }}>
          {p.status === "approved" || p.status === "held" ? "No retries — this attempt reached its final state on the first pass." : "No retries recorded."}
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 8, marginBottom: 4 }}>
          {p.repair_log.map((round, i) => (
            <div key={i} style={{ fontSize: 12, color: A.body, border: `1px solid ${A.line}`, borderRadius: 8, padding: "8px 10px" }}>
              <div style={{ fontWeight: 600, marginBottom: 3 }}>
                Round {round.attempt ?? i + 1} — Retry reason{round.gate_targeted ? ` (${round.gate_targeted})` : ""}
              </div>
              {round.violations && round.violations.length > 0 ? (
                <ul style={{ margin: 0, paddingLeft: 18, color: A.muted }}>
                  {round.violations.map((v, j) => <li key={j} style={{ fontSize: 11.5 }}>{v}</li>)}
                </ul>
              ) : (
                <div style={{ fontSize: 11.5, color: A.muted2 }}>No violation text on record for this round.</div>
              )}
            </div>
          ))}
        </div>
      )}
      {p.is_buffer_retry && (
        <div style={{ fontSize: 11.5, color: A.muted, marginTop: 6 }}>
          This attempt is also a buffer-retry of an earlier held piece for the same request
          ({p.sibling_piece_count} write attempts total for this request).
        </div>
      )}

      {/* Publish status. */}
      <SectionLabel style={{ marginTop: 14 }}>Publish</SectionLabel>
      <div style={{ fontSize: 12.5, color: A.body, display: "flex", alignItems: "center", gap: 8 }}>
        <Badge color={p.publish_status === "published" ? "green" : p.publish_status === "pending_publish" ? "amber" : "gray"}>
          {p.publish_status}
        </Badge>
        {p.publish_external_url && (
          <a href={p.publish_external_url} target="_blank" rel="noreferrer" style={{ color: A.gold }}>View published post ↗</a>
        )}
        {p.publish_published_at && (
          <span style={{ fontSize: 11.5, color: A.muted2 }}>published {new Date(p.publish_published_at).toLocaleString()}</span>
        )}
        {/* AA-560 — moved here from the deleted a4-oversight page's own Publish Log section, same
            confirm-then-call shape. Only a published row with a real publish_id can be
            unpublished — a pending/n/a row has no publish_log row to act on. */}
        {p.publish_status === "published" && p.publish_id && (
          <button
            onClick={() => onForceUnpublish(p.publish_id as string)}
            disabled={unpublishingId === p.publish_id}
            style={{
              padding: "5px 10px", borderRadius: 6, border: `1px solid ${A.red}`,
              background: "transparent", color: A.red, fontSize: 11.5, cursor: "pointer",
              opacity: unpublishingId === p.publish_id ? 0.5 : 1,
            }}
          >
            {unpublishingId === p.publish_id ? "Unpublishing…" : "Force unpublish"}
          </button>
        )}
      </div>
    </Card>
  );
}

// AA-572 point 5 — one Lineage entity per line (label + value), replacing the old single
// concatenated string.
function LineageLine({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ fontSize: 12.5, color: A.body, lineHeight: 1.7 }}>
      <strong>{label}:</strong> {children}
    </div>
  );
}

function SectionLabel({ children, style }: { children: React.ReactNode; style?: React.CSSProperties }) {
  return (
    <div style={{
      fontSize: 11, fontWeight: 600, color: A.ink3, textTransform: "uppercase",
      letterSpacing: "0.06em", marginBottom: 6, display: "flex", alignItems: "center", gap: 6, ...style,
    }}>
      <Radio size={11} style={{ opacity: 0.5 }} />{children}
    </div>
  );
}
