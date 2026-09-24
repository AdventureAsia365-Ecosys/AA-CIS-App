"use client";
// app/admin/platform-stats/page.tsx — AA-560, "07 · Platform Stats" replaces the deleted
// `/admin/a4-oversight` (Cross-Tenant Oversight) page.
//
// AA-558 Phần 3 confirmed 2/4 of the old page's sections were 100% duplicated with other pages:
// Content Log (T9/T10) and Publish Log (T11)'s READ side both moved into Content Trace (06,
// AA-568/572/575). This page keeps ONLY the section that was never duplicated anywhere —
// Review Log (T3/T5 Escalations), moved here verbatim (same endpoints, same field shapes) —
// plus one genuinely new thing: a real backend aggregate for "gate/lỗi mắc phải nhiều nhất toàn
// platform" (GET /admin/a4/platform-stats), replacing the old page's client-side
// "F1_GROUNDING × 4" tag rollup, which only ever counted whatever was in the currently-loaded
// `limit=200` page of Content Log rows (AA-558 Phần 1 Q3's own finding) — this one has no cap.
//
// Force unpublish (the old page's one mutating action) is NOT here — AA-560's own gap-finding
// (STEP0, this issue's Linear comment, 09/09/2026) confirmed that action has no other UI home and
// moved it into Content Trace (06) instead, since it acts on one specific published piece, the
// same row-level data Content Trace already displays — see that page for the button.
//
// Sub-nav (AA-575's SocialContentSubNav.tsx) + full per-column sort (AA-572→AA-575's pattern) are
// both built in from the start here, per this issue's explicit "don't repeat that lesson"
// requirement — see `ReviewSort`/`reviewSortValue` below.
//
// AA-633 (23/09/2026) — Trust Ramp section removed. Its backend (`GET/POST /admin/a4/trust-ramp*`)
// was deleted in AA-603 (S191, PR #413) — the feature operated on the dead N7/N8
// `acp_deliver.packets.publish_mode` model; the live T11 publish flow (v1_publish.py) has no
// ramp/publish-mode concept at all. This FE call site was missed at the time, causing a live
// HTTP 404 on this page. Removed here rather than restored, per the same AA-603 assessment.
import { useState, useEffect, useCallback, useMemo } from "react";
import { ChevronDown, ChevronUp } from "lucide-react";
import AdminSidebar from "../_components/AdminSidebar";
import SocialContentSubNav from "../_components/SocialContentSubNav";
import { A, serif, mono, sans, Card, Badge, LoadingScreen } from "../_components/adminUi";
import { fetchJson, EmptyState, ErrorState } from "../_components/auditPanels";

// ── Types — match GET /admin/a4/review-log, /admin/a4/platform-stats ───────────────────────────

interface EscalateDetailItem {
  check_id: string;
  field: string | null;
  description: string | null;
  source_span: string | null;
  suggested_fix: string | null;
}

interface ReviewLogRow {
  id: string;
  tour_id: string;
  tenant_id: string;
  tenant_name: string | null;
  tenant_slug: string | null;
  tenant_tour_version_id: string;
  failure_summary: string | null;
  escalate_detail: EscalateDetailItem[];
  review_status: string;
  created_at: string | null;
}

interface PlatformStats {
  total_pieces: number;
  published_count: number;
  by_status: { status: string; count: number }[];
  by_channel: { channel: string; count: number }[];
  top_gate_failures: { gate: string; fail_count: number }[];
}

// AA-615 — GET /admin/a4/gate-telemetry: the admin-only gate/severity/retry/publish signal per
// tenant/channel (the tenant never sees any of this — their view is flat ready_state, AA-613).
interface GateTelemetryTenantChannel {
  tenant_id: string;
  tenant_name: string | null;
  channel: string | null;
  total: number;
  warn_count: number;    // approved pieces carrying a non-blocking gate note (flags)
  held_count: number;    // blocked-after-retry, shipped but publish-gated
  failed_count: number;  // system error, no content produced
  retry_count: number;   // took a 2nd internal write attempt
  publish_count: number;
  export_count: number;
}

interface GateTelemetryGateFailure {
  channel: string | null;
  gate: string;
  blocking: boolean;
  fail_count: number;
}

interface GateTelemetry {
  by_tenant_channel: GateTelemetryTenantChannel[];
  top_gate_failures_by_channel: GateTelemetryGateFailure[];
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function fmtDate(s: string | null): string {
  return s ? new Date(s).toLocaleString() : "—";
}

// ── Sort — same mechanism as Content Trace (06), AA-572/575 ─────────────────────

type ReviewSortKey = "tenant" | "failure" | "checks" | "status" | "created_at";
type Sort<K extends string> = { key: K; dir: "asc" | "desc" };

function useToggleSort<K extends string>() {
  const [sort, setSort] = useState<Sort<K> | null>(null);
  const toggle = useCallback((key: K) => {
    setSort(prev => prev?.key === key ? { key, dir: prev.dir === "desc" ? "asc" : "desc" } : { key, dir: "desc" });
  }, []);
  return { sort, toggle };
}

function reviewSortValue(r: ReviewLogRow, key: ReviewSortKey): string | number {
  switch (key) {
    case "tenant": return r.tenant_name ?? r.tenant_slug ?? "";
    case "failure": return r.failure_summary ?? "";
    case "checks": return (r.escalate_detail || []).length;
    case "status": return r.review_status ?? "";
    case "created_at": return r.created_at ? new Date(r.created_at).getTime() : 0;
  }
}

type TelemetrySortKey =
  | "tenant" | "channel" | "total" | "warn" | "held" | "failed" | "retry" | "publish" | "export";

function telemetrySortValue(r: GateTelemetryTenantChannel, key: TelemetrySortKey): string | number {
  switch (key) {
    case "tenant": return r.tenant_name ?? r.tenant_id ?? "";
    case "channel": return r.channel ?? "";
    case "total": return r.total;
    case "warn": return r.warn_count;
    case "held": return r.held_count;
    case "failed": return r.failed_count;
    case "retry": return r.retry_count;
    case "publish": return r.publish_count;
    case "export": return r.export_count;
  }
}

function sortRows<T, K extends string>(rows: T[], sort: Sort<K> | null, valueOf: (r: T, k: K) => string | number): T[] {
  if (!sort) return rows;
  const withKey = rows.map(r => ({ r, k: valueOf(r, sort.key) }));
  withKey.sort((a, b) => {
    const cmp = typeof a.k === "string" && typeof b.k === "string"
      ? a.k.localeCompare(b.k)
      : (a.k as number) - (b.k as number);
    return sort.dir === "asc" ? cmp : -cmp;
  });
  return withKey.map(x => x.r);
}

function SortableTh<K extends string>({ label, sortKey, sort, onSort, style }: {
  label: string; sortKey?: K; sort: Sort<K> | null; onSort: (key: K) => void; style?: React.CSSProperties;
}) {
  const active = sortKey && sort?.key === sortKey;
  return (
    <th onClick={sortKey ? () => onSort(sortKey) : undefined} style={{
      padding: "8px 12px", textAlign: "left", fontSize: 10.5, fontWeight: 600,
      letterSpacing: "0.08em", textTransform: "uppercase", color: active ? A.gold : A.muted,
      borderBottom: `1px solid ${A.line}`, whiteSpace: "nowrap",
      cursor: sortKey ? "pointer" : "default", userSelect: "none", ...style,
    }}>
      <span style={{ display: "inline-flex", alignItems: "center", gap: 3 }}>
        {label}
        {sortKey && (
          active ? (sort!.dir === "asc" ? <ChevronUp size={12} /> : <ChevronDown size={12} />)
            : <ChevronDown size={12} style={{ opacity: 0.25 }} />
        )}
      </span>
    </th>
  );
}

// ── Review Log section — moved verbatim from a4-oversight/page.tsx, sort added ──────────────────

function ReviewLogSection() {
  const [rows, setRows] = useState<ReviewLogRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tenantFilter, setTenantFilter] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);
  const { sort, toggle } = useToggleSort<ReviewSortKey>();

  const fetchData = useCallback(() => {
    setLoading(true);
    const params = new URLSearchParams({ limit: "200" });
    if (tenantFilter.trim()) params.set("tenant_id", tenantFilter.trim());
    fetch(`/api/admin/a4/review-log?${params}`)
      .then(r => (r.ok ? r.json() : Promise.reject(`HTTP ${r.status}`)))
      .then(d => { setRows(d.data || []); setError(null); })
      .catch(e => setError(String(e)))
      .finally(() => setLoading(false));
  }, [tenantFilter]);

  useEffect(() => { fetchData(); }, [fetchData]);

  // Client-side check_id pattern rollup — unchanged from the old page (this is a small, already-
  // loaded set of T3/T5 escalation rows, not the platform-wide gate-failure aggregate below, which
  // is the one AA-558 flagged as needing a real backend query).
  const checkIdCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const row of rows) {
      for (const item of row.escalate_detail || []) {
        counts[item.check_id] = (counts[item.check_id] || 0) + 1;
      }
    }
    return Object.entries(counts).sort((a, b) => b[1] - a[1]);
  }, [rows]);

  const sortedRows = useMemo(() => sortRows(rows, sort, reviewSortValue), [rows, sort]);

  return (
    <Card style={{ marginBottom: 24 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 16 }}>
        <div>
          <h2 style={{ fontFamily: serif, fontSize: 18, fontWeight: 500, color: A.ink, margin: "0 0 4px" }}>
            Review Log — T3/T5 Escalations
          </h2>
          <div style={{ fontSize: 12, color: A.muted }}>
            T3 QA-gate failures (auto-passed to the tenant, logged here for pattern review) and T5
            atomize failures (check_id prefixed t5_atomize: — filterable via the Checks badges
            below) — neither is a queue to action, both are post-hoc pattern review.
          </div>
        </div>
        <input
          value={tenantFilter}
          onChange={e => setTenantFilter(e.target.value)}
          placeholder="Filter by tenant_id…"
          style={{
            padding: "6px 10px", borderRadius: 6, border: `1px solid ${A.line}`,
            fontSize: 12, fontFamily: mono, width: 280, outline: "none",
          }}
        />
      </div>

      {checkIdCounts.length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 16 }}>
          {checkIdCounts.map(([checkId, count]) => (
            <Badge key={checkId} color={count > 1 ? "amber" : "gray"}>
              {checkId} × {count}
            </Badge>
          ))}
        </div>
      )}

      {loading ? (
        <div style={{ padding: 24, textAlign: "center", color: A.muted }}>Loading…</div>
      ) : error ? (
        <div style={{ padding: 24, textAlign: "center", color: A.red }}>{error}</div>
      ) : rows.length === 0 ? (
        <div style={{ padding: 24, textAlign: "center", color: A.muted2 }}>No escalations found.</div>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr style={{ background: A.bg }}>
                <SortableTh label="Tenant" sortKey="tenant" sort={sort} onSort={toggle} />
                <SortableTh label="Failure Summary" sortKey="failure" sort={sort} onSort={toggle} />
                <SortableTh label="Checks" sortKey="checks" sort={sort} onSort={toggle} />
                <SortableTh label="Status" sortKey="status" sort={sort} onSort={toggle} />
                <SortableTh label="Created" sortKey="created_at" sort={sort} onSort={toggle} />
                <SortableTh label="" sort={sort} onSort={toggle} />
              </tr>
            </thead>
            <tbody>
              {sortedRows.map(row => (
                <RowLine key={row.id} row={row}
                  expanded={expanded === row.id}
                  onToggle={() => setExpanded(expanded === row.id ? null : row.id)} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

function RowLine({ row, expanded, onToggle }: {
  row: ReviewLogRow; expanded: boolean; onToggle: () => void;
}) {
  const td: React.CSSProperties = { padding: "10px 12px", borderBottom: `1px solid ${A.line}`, fontSize: 12.5, color: A.body, verticalAlign: "top" };
  return (
    <>
      <tr onClick={onToggle} style={{ cursor: "pointer" }}>
        <td style={td}>
          <div style={{ fontWeight: 600 }}>{row.tenant_name || "—"}</div>
          <div style={{ fontFamily: mono, fontSize: 10.5, color: A.muted2 }}>{row.tenant_slug || row.tenant_id.slice(0, 8)}</div>
        </td>
        <td style={td}>{row.failure_summary || "—"}</td>
        <td style={td}>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
            {(row.escalate_detail || []).map((item, i) => (
              <Badge key={i} color="gray">{item.check_id}</Badge>
            ))}
          </div>
        </td>
        <td style={td}><Badge color={row.review_status === "pending" ? "amber" : "gray"}>{row.review_status}</Badge></td>
        <td style={{ ...td, fontFamily: mono, fontSize: 11, color: A.muted }}>{fmtDate(row.created_at)}</td>
        <td style={{ ...td, textAlign: "center" }}>{expanded ? "▲" : "▼"}</td>
      </tr>
      {expanded && (
        <tr>
          <td colSpan={6} style={{ padding: "0 12px 14px", borderBottom: `1px solid ${A.line}` }}>
            <div style={{ background: A.bg, borderRadius: 8, padding: 12, fontFamily: mono, fontSize: 11, color: A.body }}>
              <div style={{ marginBottom: 6, color: A.muted2 }}>
                tour_id: {row.tour_id} · version_id: {row.tenant_tour_version_id}
              </div>
              {(row.escalate_detail || []).map((item, i) => (
                <div key={i} style={{ marginBottom: 4 }}>
                  <strong>{item.check_id}</strong>
                  {item.field && <> · field: {item.field}</>}
                  {item.description && <> — {item.description}</>}
                  {item.source_span && <div style={{ color: A.muted2, marginTop: 2 }}>“{item.source_span}”</div>}
                </div>
              ))}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

// ── Platform Stats aggregate section — NEW, real backend GROUP BY (AA-560) ──────────────────────

function PlatformStatsSection() {
  const [stats, setStats] = useState<PlatformStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    setLoading(true);
    fetchJson<{ data: PlatformStats }>("/api/admin/a4/platform-stats")
      .then(d => { setStats(d.data); setError(null); })
      .catch(e => setError(String(e.message || e)))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(); }, [load]);

  const maxGateCount = useMemo(
    () => Math.max(1, ...(stats?.top_gate_failures.map(g => g.fail_count) ?? [1])),
    [stats],
  );

  return (
    <Card style={{ marginBottom: 24 }}>
      <div style={{ marginBottom: 16 }}>
        <h2 style={{ fontFamily: serif, fontSize: 18, fontWeight: 500, color: A.ink, margin: "0 0 4px" }}>
          Platform Stats — Gate/Error Aggregate
        </h2>
        <div style={{ fontSize: 12, color: A.muted }}>
          Real backend aggregate across EVERY content piece ever written, platform-wide — not a
          client-side rollup limited to the most recently loaded page (the old Cross-Tenant
          Oversight page's "F1_GROUNDING × 4" tags only ever counted the last 200 rows loaded).
          Platform-level totals only — per-tour/Segment/Route lineage detail lives in{" "}
          <a href="/admin/tenant-activity" style={{ color: A.ink }}>Content Trace</a>.
        </div>
      </div>

      {loading ? (
        <div style={{ padding: 24, textAlign: "center", color: A.muted }}>Loading…</div>
      ) : error ? (
        <ErrorState message={error} onRetry={load} />
      ) : !stats ? (
        <EmptyState title="No stats available" body="Nothing on record yet." />
      ) : (
        <>
          <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", marginBottom: 20 }}>
            <StatTile label="Total pieces" value={stats.total_pieces} />
            <StatTile label="Published" value={stats.published_count} />
            {stats.by_status.map(s => (
              <StatTile key={s.status} label={`Status: ${s.status}`} value={s.count} />
            ))}
          </div>

          <SectionLabel>By channel</SectionLabel>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 20 }}>
            {stats.by_channel.length === 0 ? (
              <span style={{ fontSize: 12, color: A.muted2 }}>No channel data yet.</span>
            ) : stats.by_channel.map(c => (
              <Badge key={c.channel} color="gray">{c.channel} × {c.count}</Badge>
            ))}
          </div>

          <SectionLabel>Top gate failures — platform-wide, all history</SectionLabel>
          {stats.top_gate_failures.length === 0 ? (
            <div style={{ fontSize: 12, color: A.muted2 }}>No gate failures on record.</div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              {stats.top_gate_failures.map(g => (
                <div key={g.gate} style={{ display: "flex", alignItems: "center", gap: 10 }}>
                  <div style={{ width: 160, flexShrink: 0, fontSize: 12, fontFamily: mono, color: A.body }}>{g.gate}</div>
                  <div style={{ flex: 1, background: A.bg, borderRadius: 4, height: 14, overflow: "hidden" }}>
                    <div style={{
                      height: "100%", borderRadius: 4, background: A.red,
                      width: `${Math.max(4, (g.fail_count / maxGateCount) * 100)}%`,
                    }} />
                  </div>
                  <div style={{ width: 32, textAlign: "right", fontSize: 12, fontFamily: mono, color: A.muted }}>{g.fail_count}</div>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </Card>
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

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div style={{
      fontSize: 11, fontWeight: 600, color: A.ink3, textTransform: "uppercase",
      letterSpacing: "0.06em", marginBottom: 8,
    }}>
      {children}
    </div>
  );
}

// ── Gate Telemetry section — NEW (AA-615), per-tenant/channel gate/severity/retry/publish ───────

function GateTelemetrySection() {
  const [data, setData] = useState<GateTelemetry | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [channelFilter, setChannelFilter] = useState("all");
  const { sort, toggle } = useToggleSort<TelemetrySortKey>();

  const load = useCallback(() => {
    setLoading(true);
    const params = new URLSearchParams();
    if (dateFrom) params.set("date_from", dateFrom);
    if (dateTo) params.set("date_to", dateTo);
    const qs = params.toString();
    fetchJson<{ data: GateTelemetry }>(`/api/admin/a4/gate-telemetry${qs ? `?${qs}` : ""}`)
      .then(d => { setData(d.data); setError(null); })
      .catch(e => setError(String(e.message || e)))
      .finally(() => setLoading(false));
  }, [dateFrom, dateTo]);

  useEffect(() => { load(); }, [load]);

  // Channel list for the filter pills — union of both aggregates so a channel that only has
  // publishes/exports (no content_piece rows in the window) still appears.
  const channels = useMemo(() => {
    if (!data) return [];
    const set = new Set<string>();
    for (const r of data.by_tenant_channel) if (r.channel) set.add(r.channel);
    for (const g of data.top_gate_failures_by_channel) if (g.channel) set.add(g.channel);
    return Array.from(set).sort();
  }, [data]);

  const visibleRows = useMemo(() => {
    const rows = data?.by_tenant_channel ?? [];
    const filtered = channelFilter === "all" ? rows : rows.filter(r => r.channel === channelFilter);
    return sortRows(filtered, sort, telemetrySortValue);
  }, [data, channelFilter, sort]);

  const visibleGates = useMemo(() => {
    const gates = data?.top_gate_failures_by_channel ?? [];
    return channelFilter === "all" ? gates : gates.filter(g => g.channel === channelFilter);
  }, [data, channelFilter]);

  const maxGateCount = useMemo(
    () => Math.max(1, ...visibleGates.map(g => g.fail_count)),
    [visibleGates],
  );

  const td: React.CSSProperties = {
    padding: "8px 12px", borderBottom: `1px solid ${A.line}`, fontSize: 12.5, color: A.body,
  };
  const num: React.CSSProperties = { ...td, textAlign: "right", fontFamily: mono };

  return (
    <Card style={{ marginBottom: 24 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 16, gap: 12, flexWrap: "wrap" }}>
        <div>
          <h2 style={{ fontFamily: serif, fontSize: 18, fontWeight: 500, color: A.ink, margin: "0 0 4px" }}>
            Gate Telemetry — by Tenant / Channel
          </h2>
          <div style={{ fontSize: 12, color: A.muted, maxWidth: 720 }}>
            The gate/severity/retry/publish signal the tenant never sees (their view is flat —
            just &ldquo;ready&rdquo;). <strong>Warn</strong> = shipped with a non-blocking gate
            note; <strong>Held</strong> = blocked after retry, delivered but publish-gated;{" "}
            <strong>Retry</strong> = took a 2nd write attempt. Use this to spot &ldquo;gate X keeps
            failing on channel Y&rdquo; or &ldquo;tenant Z keeps shipping warn content&rdquo; and
            decide what prompt/gate/rubric to improve.
          </div>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <input
            type="date" value={dateFrom} onChange={e => setDateFrom(e.target.value)}
            aria-label="From date"
            style={{ padding: "6px 10px", borderRadius: 6, border: `1px solid ${A.line}`, fontSize: 12, fontFamily: mono, outline: "none" }}
          />
          <span style={{ fontSize: 12, color: A.muted2 }}>→</span>
          <input
            type="date" value={dateTo} onChange={e => setDateTo(e.target.value)}
            aria-label="To date"
            style={{ padding: "6px 10px", borderRadius: 6, border: `1px solid ${A.line}`, fontSize: 12, fontFamily: mono, outline: "none" }}
          />
          {(dateFrom || dateTo) && (
            <button
              onClick={() => { setDateFrom(""); setDateTo(""); }}
              style={{ padding: "6px 10px", borderRadius: 6, border: `1px solid ${A.line}`, background: "transparent", color: A.muted, fontSize: 11, cursor: "pointer" }}
            >
              Clear
            </button>
          )}
        </div>
      </div>

      {channels.length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 16 }}>
          <FilterPill label="All channels" active={channelFilter === "all"} onClick={() => setChannelFilter("all")} />
          {channels.map(c => (
            <FilterPill key={c} label={c} active={channelFilter === c} onClick={() => setChannelFilter(c)} />
          ))}
        </div>
      )}

      {loading ? (
        <div style={{ padding: 24, textAlign: "center", color: A.muted }}>Loading…</div>
      ) : error ? (
        <ErrorState message={error} onRetry={load} />
      ) : !data || data.by_tenant_channel.length === 0 ? (
        <EmptyState title="No telemetry yet" body="No content pieces in this window." />
      ) : (
        <>
          <div style={{ overflowX: "auto", marginBottom: 24 }}>
            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead>
                <tr style={{ background: A.bg }}>
                  <SortableTh label="Tenant" sortKey="tenant" sort={sort} onSort={toggle} />
                  <SortableTh label="Channel" sortKey="channel" sort={sort} onSort={toggle} />
                  <SortableTh label="Total" sortKey="total" sort={sort} onSort={toggle} style={{ textAlign: "right" }} />
                  <SortableTh label="Warn" sortKey="warn" sort={sort} onSort={toggle} style={{ textAlign: "right" }} />
                  <SortableTh label="Held" sortKey="held" sort={sort} onSort={toggle} style={{ textAlign: "right" }} />
                  <SortableTh label="Failed" sortKey="failed" sort={sort} onSort={toggle} style={{ textAlign: "right" }} />
                  <SortableTh label="Retry" sortKey="retry" sort={sort} onSort={toggle} style={{ textAlign: "right" }} />
                  <SortableTh label="Publish" sortKey="publish" sort={sort} onSort={toggle} style={{ textAlign: "right" }} />
                  <SortableTh label="Export" sortKey="export" sort={sort} onSort={toggle} style={{ textAlign: "right" }} />
                </tr>
              </thead>
              <tbody>
                {visibleRows.map(r => (
                  <tr key={`${r.tenant_id}:${r.channel ?? ""}`}>
                    <td style={td}>
                      <div style={{ fontWeight: 600 }}>{r.tenant_name || "—"}</div>
                      <div style={{ fontFamily: mono, fontSize: 10.5, color: A.muted2 }}>{r.tenant_id.slice(0, 8)}</div>
                    </td>
                    <td style={td}><Badge color="gray">{r.channel || "—"}</Badge></td>
                    <td style={num}>{r.total}</td>
                    <td style={{ ...num, color: r.warn_count > 0 ? A.amber : A.muted2 }}>{r.warn_count}</td>
                    <td style={{ ...num, color: r.held_count > 0 ? A.red : A.muted2 }}>{r.held_count}</td>
                    <td style={{ ...num, color: r.failed_count > 0 ? A.red : A.muted2 }}>{r.failed_count}</td>
                    <td style={num}>{r.retry_count}</td>
                    <td style={num}>{r.publish_count}</td>
                    <td style={num}>{r.export_count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <SectionLabel>Top gate failures{channelFilter !== "all" ? ` — ${channelFilter}` : " — by channel"}</SectionLabel>
          {visibleGates.length === 0 ? (
            <div style={{ fontSize: 12, color: A.muted2 }}>No gate failures on record for this window.</div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              {visibleGates.map((g, i) => (
                <div key={`${g.channel ?? ""}:${g.gate}:${i}`} style={{ display: "flex", alignItems: "center", gap: 10 }}>
                  <div style={{ width: 90, flexShrink: 0, fontSize: 11, fontFamily: mono, color: A.muted2 }}>{g.channel || "—"}</div>
                  <div style={{ width: 150, flexShrink: 0, fontSize: 12, fontFamily: mono, color: A.body, display: "flex", alignItems: "center", gap: 5 }}>
                    {g.gate}
                    {g.blocking
                      ? <Badge color="red">block</Badge>
                      : <Badge color="amber">warn</Badge>}
                  </div>
                  <div style={{ flex: 1, background: A.bg, borderRadius: 4, height: 14, overflow: "hidden" }}>
                    <div style={{
                      height: "100%", borderRadius: 4, background: g.blocking ? A.red : A.amber,
                      width: `${Math.max(4, (g.fail_count / maxGateCount) * 100)}%`,
                    }} />
                  </div>
                  <div style={{ width: 32, textAlign: "right", fontSize: 12, fontFamily: mono, color: A.muted }}>{g.fail_count}</div>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </Card>
  );
}

// AA-615 — small channel filter pill (self-contained; the tenant-portal ReviewList has its own).
function FilterPill({ label, active, onClick }: { label: string; active: boolean; onClick: () => void }) {
  return (
    <button onClick={onClick} style={{
      padding: "5px 12px", borderRadius: 999, border: `1px solid ${active ? A.gold : A.line}`,
      background: active ? A.card : "transparent", color: active ? A.ink : A.muted,
      fontSize: 12, fontWeight: 600, cursor: "pointer", fontFamily: sans,
    }}>
      {label}
    </button>
  );
}

// ── Main Page ─────────────────────────────────────────────────────────────────

export default function PlatformStatsPage() {
  return (
    <div style={{ display: "flex", minHeight: "100vh", background: A.bg, fontFamily: sans }}>
      <AdminSidebar />
      <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", height: "100vh" }}>
        <div style={{ flexShrink: 0, background: A.bg, padding: "28px 32px 16px", borderBottom: `1px solid ${A.line}` }}>
          <h1 style={{ fontFamily: serif, fontSize: 26, fontWeight: 500, color: A.ink, margin: 0 }}>
            07 · Platform Stats
          </h1>
          <div style={{ fontSize: 12, color: A.muted, marginTop: 4, maxWidth: 760 }}>
            Post-hoc, cross-tenant monitoring — AA does not gate tenant content at any T0-T11 step.
            Review Log is read-only pattern review; the aggregate below is a real platform-wide
            backend rollup, not a per-page-load client-side count.
          </div>
        </div>

        <div style={{ flex: 1, minHeight: 0, overflowY: "auto", overflowX: "auto", padding: "20px 32px 32px" }}>
          <div className="a527-dash-body" style={{ display: "flex", gap: 20, alignItems: "flex-start" }}>
            <SocialContentSubNav active="platform_stats" />
            <div style={{ flex: 1, minWidth: 0 }}>
              <ReviewLogSection />
              <PlatformStatsSection />
              <GateTelemetrySection />
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
