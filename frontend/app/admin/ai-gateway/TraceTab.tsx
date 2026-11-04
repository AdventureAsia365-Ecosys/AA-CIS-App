"use client";
// app/admin/ai-gateway/TraceTab.tsx — AA-1042 (S268, route control center): AI gateway › Trace.
//
// How each stage's route behaved (backend api/routers/admin_llm_route.py, from the ledger: every attempt row of one
// gateway request carries the same route trace id):
//   - Who answered, per stage: the share of answered requests per rung (the first model on its first provider =
//     primary; any later model or provider = fallback), the fallback share and the requests with no answer;
//   - Requests: one line per gateway request (filter by stage, only the ones that needed a fallback); a line opens
//     RouteTraceDrawer with every attempt, its reason, latency and cost;
//   - Content written by a fallback model (QA, admin only: tenants never see it): marked, or held for review. The
//     catalog (A1) list is open to the tab's readers; the tenant lists (T2 versions, T9 pieces) come from the Tenant
//     QA read /admin/content-quality/fallback-writes and show only to a role with tenant_content.read;
//   - Route alerts (RouteAlertsCard): thresholds, the hold switch, Run check now, the route test.
// The drawer also opens from the address (?tab=trace&rt=<trace id>&rt_at=<time>), which a Calls row's "Route" link
// sets. Built on the kit only (Section, SegmentedControl, DataTable, Badge, Select, Checkbox, LinkButton).

import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import {
  Badge, Checkbox, DataTable, DateText, K, LinkButton, Section, SegmentedControl, Select, Spinner, apiGet, mono,
  useAdminIdentity, useT, type ColumnDef,
} from "../../_kit";
import { usd } from "@/lib/aiGateway";
import { can } from "@/lib/rbac";
import {
  FALLBACK_CONTENT_API, FALLBACK_TENANT_CONTENT_API, ROUTE_KEY, ROUTE_WINDOWS, outcomeTone, pct, requestsPath,
  rungLabel, rungsPath, type FallbackContentResp, type FallbackPiece, type FallbackTenantContentResp,
  type FallbackTenantVersion, type FallbackTour, type RouteRequest, type RouteRequestsResp, type RouteTarget,
  type RouteWindow, type RungsResp, type StageRungs,
} from "@/lib/routeTrace";
import RouteAlertsCard from "./RouteAlertsCard";
import RouteTraceDrawer from "./RouteTraceDrawer";
import { RT, RT_OUTCOME, RT_RUNG, RT_WINDOW } from "./routeTrace.messages";

const small = { fontSize: 12, color: K.muted, lineHeight: 1.45 } as const;
const num = { fontFamily: mono, fontSize: 12.5, whiteSpace: "nowrap" } as const;

function RungList({ s }: { s: StageRungs }) {
  const tr = useT(RT_RUNG);
  return (
    <span style={{ display: "grid", gap: 3 }}>
      {s.rungs.map((r) => (
        <span key={`${r.rung}-${r.model}-${r.connection}`}
          style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap", fontSize: 12 }}>
          <Badge tone={r.rung === "primary" ? "success" : r.rung === "fallback" ? "warning" : "neutral"} dot={false}>
            {tr(r.rung)}</Badge>
          <span style={{ fontFamily: mono, overflowWrap: "anywhere" }}>{rungLabel(r.model, r.connection)}</span>
          <span style={{ fontFamily: mono, color: K.ink }}>{pct(r.share_pct)}</span>
          <span style={{ color: K.muted }}>({r.answered})</span>
        </span>
      ))}
    </span>
  );
}

export default function TraceTab({ target, onOpen }: {
  target: RouteTarget | null;
  onOpen: (t: RouteTarget | null) => void;
}) {
  const t = useT(RT);
  const tw = useT(RT_WINDOW);
  const to = useT(RT_OUTCOME);
  const [period, setPeriod] = useState<RouteWindow>("24h");
  const [stage, setStage] = useState<string>("");
  const [fallbackOnly, setFallbackOnly] = useState(false);

  const rungsQ = useQuery({
    queryKey: [...ROUTE_KEY, "rungs", period],
    queryFn: () => apiGet<RungsResp>(rungsPath(period), { admin: true }),
    refetchInterval: 60_000,
    retry: false,
  });
  const reqQ = useQuery({
    queryKey: [...ROUTE_KEY, "requests", period, stage, fallbackOnly],
    queryFn: () => apiGet<RouteRequestsResp>(requestsPath(period, stage || null, fallbackOnly), { admin: true }),
    refetchInterval: 60_000,
    retry: false,
  });
  const contentQ = useQuery({
    queryKey: [...ROUTE_KEY, "content", period],
    queryFn: () => apiGet<FallbackContentResp>(`${FALLBACK_CONTENT_API}?window=${period}`, { admin: true }),
    retry: false,
  });
  // Tenant QA data (every tenant's versions and pieces): only a role with tenant_content.read asks for it (the
  // backend refuses the others).
  const canTenant = can(useAdminIdentity().role, "tenant_content.read");
  const tenantQ = useQuery({
    queryKey: [...ROUTE_KEY, "tenant-content", period],
    queryFn: () => apiGet<FallbackTenantContentResp>(`${FALLBACK_TENANT_CONTENT_API}?window=${period}`,
      { admin: true }),
    enabled: canTenant,
    retry: false,
  });

  const stages = useMemo(() => (rungsQ.data?.stages ?? []).map((s) => s.stage).sort(), [rungsQ.data]);

  const rungCols = useMemo<ColumnDef<StageRungs, unknown>[]>(() => [
    { id: "stage", header: t("colStage"), accessorKey: "stage",
      cell: ({ row: { original: s } }) => (
        <span style={{ display: "grid", gap: 2 }}>
          <span style={{ fontFamily: mono, fontSize: 12.5, overflowWrap: "anywhere" }}>{s.stage}</span>
          {s.route.length > 0 && <span style={{ ...small, fontFamily: mono, overflowWrap: "anywhere" }}
            title={t("colRoute")}>{s.route.join(" → ")}</span>}
        </span>
      ) },
    { id: "answered", header: t("colAnswered"), accessorKey: "answered", meta: { align: "right" },
      cell: ({ row }) => <span style={num}>{row.original.answered}</span> },
    { id: "fallback", header: t("colFallback"), accessorFn: (s) => s.fallback_pct ?? -1, meta: { align: "right" },
      cell: ({ row: { original: s } }) => (
        <span style={{ ...num, color: (s.fallback_pct ?? 0) > 0 ? K.amber : K.body }}>
          {pct(s.fallback_pct)} <span style={{ color: K.muted }}>({s.fallback_answered})</span></span>
      ) },
    { id: "lost", header: t("colLost"), accessorKey: "lost_requests", meta: { align: "right" },
      cell: ({ row }) => <span style={{ ...num, color: row.original.lost_requests > 0 ? K.danger : K.body }}>
        {row.original.lost_requests}</span> },
    { id: "rungs", header: t("colRungs"), enableSorting: false,
      cell: ({ row }) => <RungList s={row.original} /> },
  ], [t]);

  const reqCols = useMemo<ColumnDef<RouteRequest, unknown>[]>(() => [
    { id: "at", header: t("colWhen"), accessorFn: (r) => r.last_at ?? "",
      cell: ({ row }) => <span style={{ whiteSpace: "nowrap" }}>
        {row.original.last_at ? <DateText value={row.original.last_at} withTime /> : "—"}</span> },
    { id: "stage", header: t("colStage"), accessorFn: (r) => r.stage ?? "",
      cell: ({ row: { original: r } }) => (
        <span style={{ display: "inline-flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
          <span style={{ fontFamily: mono, fontSize: 12.5, overflowWrap: "anywhere" }}>{r.stage ?? "—"}</span>
          {r.route_test && <Badge tone="info" dot={false}>{t("testBadge")}</Badge>}
        </span>
      ) },
    { id: "outcome", header: t("colOutcome"), accessorFn: (r) => r.outcome,
      cell: ({ row }) => <Badge tone={outcomeTone(row.original.outcome)}>{to(row.original.outcome)}</Badge> },
    { id: "attempts", header: t("colAttempts"), accessorFn: (r) => r.attempts ?? 0, meta: { align: "right" },
      cell: ({ row }) => <span style={num}>{row.original.attempts ?? "—"}</span> },
    { id: "served", header: t("colServedBy"), accessorFn: (r) => r.served_model ?? "",
      cell: ({ row: { original: r } }) => <span style={{ fontFamily: mono, fontSize: 12, overflowWrap: "anywhere" }}>
        {rungLabel(r.served_model, r.served_connection)}</span> },
    { id: "cost", header: t("colCost"), accessorFn: (r) => r.cost_usd ?? 0, meta: { align: "right" },
      cell: ({ row }) => <span style={num}>{usd(row.original.cost_usd)}</span> },
    { id: "latency", header: t("colLatency"), accessorFn: (r) => r.latency_ms ?? -1, meta: { align: "right" },
      cell: ({ row }) => <span style={num}>{row.original.latency_ms == null ? "—" : `${row.original.latency_ms} ms`}
      </span> },
    { id: "open", header: "", enableSorting: false,
      cell: ({ row: { original: r } }) => (
        <span data-testid="route-request-open" style={{ display: "inline-flex" }}>
          <LinkButton onClick={() => onOpen({ id: r.trace_id, at: r.last_at })}>{t("open")}</LinkButton></span>
      ) },
  ], [t, to, onOpen]);

  const tourCols = useMemo<ColumnDef<FallbackTour, unknown>[]>(() => [
    { id: "at", header: t("colWhen"), accessorFn: (r) => r.at ?? "",
      cell: ({ row }) => <span style={{ whiteSpace: "nowrap" }}>
        {row.original.at ? <DateText value={row.original.at} withTime /> : "—"}</span> },
    { id: "tour", header: t("colTour"), accessorFn: (r) => r.tour_name ?? "",
      cell: ({ row: { original: r } }) => (
        <a href={`/admin/master-content?tour=${encodeURIComponent(r.tour_id)}`} title={t("openTour")}
          style={{ color: K.accent, fontSize: 12.5, overflowWrap: "anywhere" }}>{r.tour_name ?? t("colTour")}</a>
      ) },
    { id: "version", header: t("colVersion"), accessorFn: (r) => r.version ?? 0, meta: { align: "right" },
      cell: ({ row }) => <span style={num}>{row.original.version ?? "—"}</span> },
    { id: "state", header: t("colStatus"), accessorFn: (r) => (r.held ? 1 : 0),
      cell: ({ row }) => <Badge tone={row.original.held ? "warning" : "info"} dot={false}>
        {row.original.held ? t("held") : t("marked")}</Badge> },
  ], [t]);

  const versionCols = useMemo<ColumnDef<FallbackTenantVersion, unknown>[]>(() => [
    { id: "at", header: t("colWhen"), accessorFn: (r) => r.at ?? "",
      cell: ({ row }) => <span style={{ whiteSpace: "nowrap" }}>
        {row.original.at ? <DateText value={row.original.at} withTime /> : "—"}</span> },
    { id: "tenant", header: t("colTenant"), accessorFn: (r) => r.tenant_name ?? "",
      cell: ({ row }) => <span style={{ fontSize: 12.5 }}>{row.original.tenant_name ?? "—"}</span> },
    { id: "tour", header: t("colTour"), accessorFn: (r) => r.tour_name ?? "",
      cell: ({ row }) => <span style={{ fontSize: 12.5, overflowWrap: "anywhere" }}>{row.original.tour_name ?? "—"}
      </span> },
    { id: "version", header: t("colVersion"), accessorFn: (r) => r.version ?? 0, meta: { align: "right" },
      cell: ({ row }) => <span style={num}>{row.original.version ?? "—"}</span> },
    { id: "state", header: t("colStatus"), accessorFn: (r) => (r.held ? 1 : 0),
      cell: ({ row }) => <Badge tone={row.original.held ? "warning" : "info"} dot={false}>
        {row.original.held ? t("held") : t("marked")}</Badge> },
  ], [t]);

  const pieceCols = useMemo<ColumnDef<FallbackPiece, unknown>[]>(() => [
    { id: "at", header: t("colWhen"), accessorFn: (r) => r.at ?? "",
      cell: ({ row }) => <span style={{ whiteSpace: "nowrap" }}>
        {row.original.at ? <DateText value={row.original.at} withTime /> : "—"}</span> },
    { id: "tenant", header: t("colTenant"), accessorFn: (r) => r.tenant_name ?? "",
      cell: ({ row }) => <span style={{ fontSize: 12.5 }}>{row.original.tenant_name ?? "—"}</span> },
    { id: "channel", header: t("colChannel"), accessorFn: (r) => r.channel ?? "",
      cell: ({ row }) => <span style={{ fontFamily: mono, fontSize: 12 }}>{row.original.channel ?? "—"}</span> },
    { id: "state", header: t("colStatus"), accessorFn: (r) => (r.held ? 1 : 0),
      cell: ({ row }) => <Badge tone={row.original.held ? "warning" : "info"} dot={false}>
        {row.original.held ? t("held") : t("marked")}</Badge> },
  ], [t]);

  const windowControl = (
    <SegmentedControl label={t("window")} value={period} onChange={(v) => setPeriod(v as RouteWindow)} size="sm"
      testId="route-window" options={ROUTE_WINDOWS.map((w) => ({ value: w, label: tw(w) }))} />
  );

  const content = contentQ.data;
  const tenant = canTenant ? tenantQ.data : undefined;
  const nothing = !!content && content.tours.length === 0
    && (!canTenant || (!!tenant && tenant.versions.length === 0 && tenant.pieces.length === 0));
  return (
    <div data-testid="ai-gateway-trace-tab" style={{ display: "grid", gap: 16, minWidth: 0 }}>
      <Section testId="route-rungs" title={t("rungsTitle")} description={t("rungsIntro")} actions={windowControl}>
        {rungsQ.isLoading && <Spinner />}
        {rungsQ.isError && <div style={{ fontSize: 12.5, color: K.danger }}>{t("loadError")}</div>}
        {rungsQ.data && (
          <DataTable tableId="route-rungs" data={rungsQ.data.stages} columns={rungCols} getRowId={(s) => s.stage}
            compact tableMinWidth={760} paginate={rungsQ.data.stages.length > 25} emptyTitle={t("rungsEmpty")} />
        )}
      </Section>

      <Section testId="route-requests" title={t("requestsTitle")} description={t("requestsIntro")}>
        <div style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "center", marginBottom: 10 }}>
          <Select value={stage} onChange={(e) => setStage(e.target.value)} aria-label={t("filterStage")}
            options={[{ value: "", label: t("allStages") }, ...stages.map((s) => ({ value: s, label: s }))]}
            style={{ maxWidth: "100%", minWidth: 0, width: 260 }} data-testid="route-stage-filter" />
          <Checkbox checked={fallbackOnly} onChange={(e) => setFallbackOnly(e.target.checked)}
            label={t("onlyFallbacks")} />
        </div>
        {reqQ.isLoading && <Spinner />}
        {reqQ.isError && <div style={{ fontSize: 12.5, color: K.danger }}>{t("loadError")}</div>}
        {reqQ.data && (
          <DataTable tableId="route-requests" data={reqQ.data.requests} columns={reqCols}
            getRowId={(r) => r.trace_id} compact tableMinWidth={900} emptyTitle={t("requestsEmpty")} />
        )}
      </Section>

      <Section testId="route-fallback-content" title={t("contentTitle")} description={t("contentIntro")}
        actions={canTenant ? <a href="/admin/tenant-qa/quality" style={{ color: K.accent, fontSize: 12.5 }}>
          {t("openQuality")}</a> : undefined}>
        {(contentQ.isLoading || (canTenant && tenantQ.isLoading)) && <Spinner />}
        {(contentQ.isError || (canTenant && tenantQ.isError)) && (
          <div style={{ fontSize: 12.5, color: K.danger }}>{t("loadError")}</div>
        )}
        {nothing && <div style={small}>{t("contentEmpty")}</div>}
        {content && content.tours.length > 0 && (
          <div style={{ marginBottom: 12 }}>
            <div style={{ fontSize: 12.5, fontWeight: 600, color: K.ink, marginBottom: 6 }}>{t("contentTours")}</div>
            <DataTable tableId="route-fallback-tours" data={content.tours} columns={tourCols}
              getRowId={(r) => r.id} compact tableMinWidth={560} />
          </div>
        )}
        {tenant && tenant.versions.length > 0 && (
          <div style={{ marginBottom: 12 }}>
            <div style={{ fontSize: 12.5, fontWeight: 600, color: K.ink, marginBottom: 6 }}>
              {t("contentTenantVersions")}</div>
            <DataTable tableId="route-fallback-versions" data={tenant.versions} columns={versionCols}
              getRowId={(r) => r.id} compact tableMinWidth={640} />
          </div>
        )}
        {tenant && tenant.pieces.length > 0 && (
          <div>
            <div style={{ fontSize: 12.5, fontWeight: 600, color: K.ink, marginBottom: 6 }}>{t("contentPieces")}</div>
            <DataTable tableId="route-fallback-pieces" data={tenant.pieces} columns={pieceCols}
              getRowId={(r) => r.piece_id} compact tableMinWidth={560} />
          </div>
        )}
      </Section>

      <RouteAlertsCard onOpenRequest={(id, at) => onOpen({ id, at })} />

      {target && <RouteTraceDrawer key={`${target.id}-${target.at ?? ""}`} target={target}
        onClose={() => onOpen(null)} />}
    </div>
  );
}
