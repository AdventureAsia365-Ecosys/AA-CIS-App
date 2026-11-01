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
import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import SocialContentSubNav, { SocialContentSplit } from "../_components/SocialContentSubNav";
import { TenantSelect } from "../_components/TenantSelect";
import {
  PageFrame, PageFrameBody, PageFrameHeader, PageHeader, useFormat, useT, Badge, K, Section, mono,
  serif, Button, ChipButton, DataTable, Drawer, Input, type ColumnDef,
} from "../../_kit";
import { STATS } from "./messages";
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

// ── Sort — same mechanism as Content Trace (06), AA-572/575 ─────────────────────

type ReviewSortKey = "tenant" | "failure" | "checks" | "status" | "created_at";
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

// ── Review Log section — moved verbatim from a4-oversight/page.tsx, sort added ──────────────────

function ReviewLogSection() {
  const t = useT(STATS);
  const { formatDateTime } = useFormat();
  const [tenantFilter, setTenantFilter] = useState("");
  // AA-1006: the escalation detail opens in a kit Drawer (the table row used to expand in place).
  const [open, setOpen] = useState<ReviewLogRow | null>(null);
  const q = useQuery({
    queryKey: ["a4-review-log", tenantFilter],
    queryFn: () => {
      const params = new URLSearchParams({ limit: "200" });
      if (tenantFilter) params.set("tenant_id", tenantFilter);
      return fetch(`/api/admin/a4/review-log?${params}`)
        .then(r => (r.ok ? r.json() : Promise.reject(`HTTP ${r.status}`)))
        .then(d => (d.data || []) as ReviewLogRow[]);
    },
  });
  const rows = useMemo(() => q.data ?? [], [q.data]);
  const loading = q.isPending;
  const error = q.isError ? String(q.error) : null;

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

  const columns = useMemo<ColumnDef<ReviewLogRow, unknown>[]>(() => [
    { id: "tenant", header: t("colTenant"), accessorFn: (r) => reviewSortValue(r, "tenant"), cell: ({ row: { original: r } }) => (
      <div>
        <div style={{ fontWeight: 600 }}>{r.tenant_name || "—"}</div>
        <div title={r.tenant_id} style={{ fontFamily: mono, fontSize: 10.5, color: K.muted2 }}>{r.tenant_slug || r.tenant_id.slice(0, 8)}</div>
      </div>
    ) },
    { id: "failure", header: t("colFailure"), accessorFn: (r) => reviewSortValue(r, "failure"),
      cell: ({ row: { original: r } }) => r.failure_summary || "—" },
    { id: "checks", header: t("colChecks"), accessorFn: (r) => reviewSortValue(r, "checks"), cell: ({ row: { original: r } }) => (
      <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
        {(r.escalate_detail || []).map((item, i) => <Badge key={i} tone="neutral">{item.check_id}</Badge>)}
      </div>
    ) },
    { id: "status", header: t("colStatus"), accessorFn: (r) => reviewSortValue(r, "status"), cell: ({ row: { original: r } }) => (
      <Badge tone={r.review_status === "pending" ? "warning" : "neutral"}>{r.review_status}</Badge>
    ) },
    { id: "created_at", header: t("colCreated"), accessorFn: (r) => reviewSortValue(r, "created_at"), sortDescFirst: true,
      cell: ({ row: { original: r } }) => <span style={{ fontFamily: mono, fontSize: 11, color: K.muted }}>{formatDateTime(r.created_at)}</span> },
  ], [t, formatDateTime]);

  return (
    <Section style={{ marginBottom: 24 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", flexWrap: "wrap", gap: 12, marginBottom: 16 }}>
        <div style={{ flex: "1 1 320px", minWidth: 0 }}>
          <h2 style={{ fontFamily: serif, fontSize: 18, fontWeight: 500, color: K.ink, margin: "0 0 4px" }}>
            {t("reviewTitle")}
          </h2>
          <div style={{ fontSize: 12, color: K.muted }}>
            {t("reviewIntro")}
          </div>
        </div>
        {/* AA-919 S254: pick the tenant by name (shared admin picker) instead of typing its id. Inactive tenants
            stay offered: their old escalations are still in the log. */}
        <TenantSelect inline includeInactive value={tenantFilter} onChange={setTenantFilter} placeholder={t("allTenants")} />
      </div>

      {checkIdCounts.length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 16 }}>
          {checkIdCounts.map(([checkId, count]) => (
            <Badge key={checkId} tone={count > 1 ? "warning" : "neutral"}>
              {checkId} × {count}
            </Badge>
          ))}
        </div>
      )}

      {loading ? (
        <div style={{ padding: 24, textAlign: "center", color: K.muted }}>{t("loading")}</div>
      ) : error ? (
        <div style={{ padding: 24, textAlign: "center", color: K.danger }}>{error}</div>
      ) : rows.length === 0 ? (
        // AA-601 — deterministic "content rendered" signal for the UI smoke: appears only after the
        // review-log load resolves (never on the Loading… state), so a blank/broken page still fails.
        <div data-testid="admin-content-ready" style={{ padding: 24, textAlign: "center", color: K.muted2 }}>{t("noEscalations")}</div>
      ) : (
        <div data-testid="admin-content-ready">
          <DataTable<ReviewLogRow>
            tableId="a4-review-log"
            data={rows}
            columns={columns}
            getRowId={(r) => r.id}
            onRowClick={setOpen}
            searchable={false}
            paginate={false}
            compact
          />
        </div>
      )}
      <Drawer open={open != null} onClose={() => setOpen(null)} title={open?.tenant_name || open?.tenant_slug || "—"}>
        {open && (
          <div style={{ background: K.bg, borderRadius: 8, padding: 12, fontFamily: mono, fontSize: 11, color: K.body }}>
            <div style={{ marginBottom: 6, color: K.muted2 }}>
              tour_id: {open.tour_id} · version_id: {open.tenant_tour_version_id}
            </div>
            {(open.escalate_detail || []).map((item, i) => (
              <div key={i} style={{ marginBottom: 4 }}>
                <strong>{item.check_id}</strong>
                {item.field && <> · {t("field")} {item.field}</>}
                {item.description && <> — {item.description}</>}
                {item.source_span && <div style={{ color: K.muted2, marginTop: 2 }}>“{item.source_span}”</div>}
              </div>
            ))}
          </div>
        )}
      </Drawer>
    </Section>
  );
}

// ── Platform Stats aggregate section — NEW, real backend GROUP BY (AA-560) ──────────────────────

function PlatformStatsSection() {
  const t = useT(STATS);
  const q = useQuery({
    queryKey: ["a4-platform-stats"],
    queryFn: () => fetchJson<{ data: PlatformStats }>("/api/admin/a4/platform-stats").then(d => d.data),
  });
  const stats = q.data ?? null;
  const loading = q.isPending;
  const error = q.isError ? String((q.error as Error).message || q.error) : null;
  const load = () => { q.refetch(); };

  const maxGateCount = useMemo(
    () => Math.max(1, ...(stats?.top_gate_failures.map(g => g.fail_count) ?? [1])),
    [stats],
  );

  return (
    <Section style={{ marginBottom: 24 }}>
      <div style={{ marginBottom: 16 }}>
        <h2 style={{ fontFamily: serif, fontSize: 18, fontWeight: 500, color: K.ink, margin: "0 0 4px" }}>
          {t("aggTitle")}
        </h2>
        <div style={{ fontSize: 12, color: K.muted }}>
          {t("aggIntro")}{" "}
          <a href="/admin/tenant-activity" style={{ color: K.ink }}>{t("contentTrace")}</a>.
        </div>
      </div>

      {loading ? (
        <div style={{ padding: 24, textAlign: "center", color: K.muted }}>{t("loading")}</div>
      ) : error ? (
        <ErrorState message={error} onRetry={load} />
      ) : !stats ? (
        <EmptyState title={t("noStats")} body={t("nothingYet")} />
      ) : (
        <>
          <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", marginBottom: 20 }}>
            <StatTile label={t("totalPieces")} value={stats.total_pieces} />
            <StatTile label={t("published")} value={stats.published_count} />
            {stats.by_status.map(s => (
              <StatTile key={s.status} label={t("statusTile", s.status)} value={s.count} />
            ))}
          </div>

          <SectionLabel>{t("byChannel")}</SectionLabel>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 20 }}>
            {stats.by_channel.length === 0 ? (
              <span style={{ fontSize: 12, color: K.muted2 }}>{t("noChannel")}</span>
            ) : stats.by_channel.map(c => (
              <Badge key={c.channel} tone="neutral">{c.channel} × {c.count}</Badge>
            ))}
          </div>

          <SectionLabel>{t("topGatesAll")}</SectionLabel>
          {stats.top_gate_failures.length === 0 ? (
            <div style={{ fontSize: 12, color: K.muted2 }}>{t("noGateFailures")}</div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              {stats.top_gate_failures.map(g => (
                <div key={g.gate} style={{ display: "flex", alignItems: "center", gap: 10 }}>
                  <div style={{ width: 160, flexShrink: 0, fontSize: 12, fontFamily: mono, color: K.body }}>{g.gate}</div>
                  <div style={{ flex: 1, background: K.bg, borderRadius: 4, height: 14, overflow: "hidden" }}>
                    <div style={{
                      height: "100%", borderRadius: 4, background: K.danger,
                      width: `${Math.max(4, (g.fail_count / maxGateCount) * 100)}%`,
                    }} />
                  </div>
                  <div style={{ width: 32, textAlign: "right", fontSize: 12, fontFamily: mono, color: K.muted }}>{g.fail_count}</div>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </Section>
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

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div style={{
      fontSize: 11, fontWeight: 600, color: K.ink3, textTransform: "uppercase",
      letterSpacing: "0.06em", marginBottom: 8,
    }}>
      {children}
    </div>
  );
}

// ── Gate Telemetry section — NEW (AA-615), per-tenant/channel gate/severity/retry/publish ───────

function GateTelemetrySection() {
  const t = useT(STATS);
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [channelFilter, setChannelFilter] = useState("all");
  const q = useQuery({
    queryKey: ["a4-gate-telemetry", dateFrom, dateTo],
    queryFn: () => {
      const params = new URLSearchParams();
      if (dateFrom) params.set("date_from", dateFrom);
      if (dateTo) params.set("date_to", dateTo);
      const qs = params.toString();
      return fetchJson<{ data: GateTelemetry }>(`/api/admin/a4/gate-telemetry${qs ? `?${qs}` : ""}`).then(d => d.data);
    },
  });
  const data = q.data ?? null;
  const loading = q.isPending;
  const error = q.isError ? String((q.error as Error).message || q.error) : null;
  const load = () => { q.refetch(); };

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
    return channelFilter === "all" ? rows : rows.filter(r => r.channel === channelFilter);
  }, [data, channelFilter]);

  const visibleGates = useMemo(() => {
    const gates = data?.top_gate_failures_by_channel ?? [];
    return channelFilter === "all" ? gates : gates.filter(g => g.channel === channelFilter);
  }, [data, channelFilter]);

  const maxGateCount = useMemo(
    () => Math.max(1, ...visibleGates.map(g => g.fail_count)),
    [visibleGates],
  );

  const numCol = (key: TelemetrySortKey, label: string, color?: (r: GateTelemetryTenantChannel) => string | undefined): ColumnDef<GateTelemetryTenantChannel, unknown> => ({
    id: key, header: label, accessorFn: (r) => telemetrySortValue(r, key), sortDescFirst: true, meta: { align: "right" },
    cell: ({ row: { original: r } }) => (
      <span style={{ fontFamily: mono, color: color?.(r) }}>{telemetrySortValue(r, key)}</span>
    ),
  });
  const columns: ColumnDef<GateTelemetryTenantChannel, unknown>[] = [
    { id: "tenant", header: t("colTenant"), accessorFn: (r) => telemetrySortValue(r, "tenant"), cell: ({ row: { original: r } }) => (
      <div>
        <div style={{ fontWeight: 600 }}>{r.tenant_name || "—"}</div>
        <div title={r.tenant_id} style={{ fontFamily: mono, fontSize: 10.5, color: K.muted2 }}>{r.tenant_id.slice(0, 8)}</div>
      </div>
    ) },
    { id: "channel", header: t("colChannel"), accessorFn: (r) => telemetrySortValue(r, "channel"),
      cell: ({ row: { original: r } }) => <Badge tone="neutral">{r.channel || "—"}</Badge> },
    numCol("total", t("colTotal")),
    numCol("warn", t("warn"), (r) => (r.warn_count > 0 ? K.amber : K.muted2)),
    numCol("held", t("held"), (r) => (r.held_count > 0 ? K.danger : K.muted2)),
    numCol("failed", t("colFailed"), (r) => (r.failed_count > 0 ? K.danger : K.muted2)),
    numCol("retry", t("retry")),
    numCol("publish", t("colPublish")),
    numCol("export", t("colExport")),
  ];

  return (
    <Section style={{ marginBottom: 24 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 16, gap: 12, flexWrap: "wrap" }}>
        <div>
          <h2 style={{ fontFamily: serif, fontSize: 18, fontWeight: 500, color: K.ink, margin: "0 0 4px" }}>
            {t("telemetryTitle")}
          </h2>
          <div style={{ fontSize: 12, color: K.muted, maxWidth: 720 }}>
            {t("telemetryIntro1")} <strong>{t("warn")}</strong> {t("warnMeans")}{" "}
            <strong>{t("held")}</strong> {t("heldMeans")}{" "}
            <strong>{t("retry")}</strong> {t("retryMeans")}
          </div>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <Input
            type="date" value={dateFrom} onChange={e => setDateFrom(e.target.value)}
            aria-label={t("fromDate")}
            size="sm" style={{ fontFamily: mono, fontSize: 12 }}
          />
          <span style={{ fontSize: 12, color: K.muted2 }}>→</span>
          <Input
            type="date" value={dateTo} onChange={e => setDateTo(e.target.value)}
            aria-label={t("toDate")}
            size="sm" style={{ fontFamily: mono, fontSize: 12 }}
          />
          {(dateFrom || dateTo) && (
            <Button size="sm" variant="ghost" onClick={() => { setDateFrom(""); setDateTo(""); }}>
              {t("clear")}
            </Button>
          )}
        </div>
      </div>

      {channels.length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 16 }}>
          <FilterPill label={t("allChannels")} active={channelFilter === "all"} onClick={() => setChannelFilter("all")} />
          {channels.map(c => (
            <FilterPill key={c} label={c} active={channelFilter === c} onClick={() => setChannelFilter(c)} />
          ))}
        </div>
      )}

      {loading ? (
        <div style={{ padding: 24, textAlign: "center", color: K.muted }}>{t("loading")}</div>
      ) : error ? (
        <ErrorState message={error} onRetry={load} />
      ) : !data || data.by_tenant_channel.length === 0 ? (
        <EmptyState title={t("noTelemetry")} body={t("noPieces")} />
      ) : (
        <>
          <div style={{ marginBottom: 24 }}>
            <DataTable<GateTelemetryTenantChannel>
              tableId="a4-gate-telemetry"
              data={visibleRows}
              columns={columns}
              getRowId={(r) => `${r.tenant_id}:${r.channel ?? ""}`}
              searchable={false}
              paginate={false}
              compact
            />
          </div>

          <SectionLabel>{channelFilter !== "all" ? t("topGatesFor", channelFilter) : t("topGatesByChannel")}</SectionLabel>
          {visibleGates.length === 0 ? (
            <div style={{ fontSize: 12, color: K.muted2 }}>{t("noGateFailuresWindow")}</div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              {visibleGates.map((g, i) => (
                <div key={`${g.channel ?? ""}:${g.gate}:${i}`} style={{ display: "flex", alignItems: "center", gap: 10 }}>
                  <div style={{ width: 90, flexShrink: 0, fontSize: 11, fontFamily: mono, color: K.muted2 }}>{g.channel || "—"}</div>
                  <div style={{ width: 150, flexShrink: 0, fontSize: 12, fontFamily: mono, color: K.body, display: "flex", alignItems: "center", gap: 5 }}>
                    {g.gate}
                    {g.blocking
                      ? <Badge tone="danger">{t("block")}</Badge>
                      : <Badge tone="warning">{t("warnBadge")}</Badge>}
                  </div>
                  <div style={{ flex: 1, background: K.bg, borderRadius: 4, height: 14, overflow: "hidden" }}>
                    <div style={{
                      height: "100%", borderRadius: 4, background: g.blocking ? K.danger : K.amber,
                      width: `${Math.max(4, (g.fail_count / maxGateCount) * 100)}%`,
                    }} />
                  </div>
                  <div style={{ width: 32, textAlign: "right", fontSize: 12, fontFamily: mono, color: K.muted }}>{g.fail_count}</div>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </Section>
  );
}

// AA-615 — the channel filter pill: one kit chip (AA-1006).
function FilterPill({ label, active, onClick }: { label: string; active: boolean; onClick: () => void }) {
  return <ChipButton size="sm" selected={active} onClick={onClick}>{label}</ChipButton>;
}

// ── Main Page ─────────────────────────────────────────────────────────────────

export default function PlatformStatsPage() {
  const t = useT(STATS);
  return (
    <PageFrame variant="split">
        <PageFrameHeader>
          <PageHeader
            title={t("pageTitle")}
            description={t("pageDescription")}
          />
        </PageFrameHeader>

        <PageFrameBody scroll style={{ overflowX: "auto" }}>
          <SocialContentSplit>
            <SocialContentSubNav active="platform_stats" />
            <div style={{ flex: 1, minWidth: 0 }}>
              <ReviewLogSection />
              <PlatformStatsSection />
              <GateTelemetrySection />
            </div>
          </SocialContentSplit>
        </PageFrameBody>
    </PageFrame>
  );
}
