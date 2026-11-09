"use client";
// app/admin/ai-gateway/GuideTab.tsx — AA-1042 (S270): AI gateway › Guide.
//
// One source for the prose and live tables under it (tech lead S267): the gateway's reference page (root
// docs/architecture/ai-gateway.md and its Vietnamese copy, from the docs catalog that /admin/docs reads, no second
// copy) rendered with the kit MarkdownView, then the live state read from GET /api/admin/ai-gateway/guide (backend
// api/services/ai_gateway_guide.py, spend.read): routes with the deployment that serves each stage first (the Route
// control preview's resolver, refused deployments skipped), the model x connection matrix, the connections with their
// key NAMES, the connection caps and the stage coverage. Editing a route changes these tables, never the page.
//
// The short page guide (guide.ts / guide.vi.ts, a kit GuidePanel passed in by AiGateway.tsx) stays on top as "How to
// read this page". Language: the app language
// (English by default); a page without a copy in it shows English with a note. Links in the page open /admin/docs
// for a viewer who may read it (backend flag + the page rule of the role), anything else is plain text
// (lib/aiGatewayGuide guideLink). Issue references in the page are left out of the text shown (stripIssueIdsMarkdown).
// Kit only (ADR 0016). testIds: ai-gateway-guide (the tab), ai-gateway-guide-doc, ai-gateway-guide-live-ready.

import { useQuery } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useCallback, useMemo, type ReactNode } from "react";
import {
  Badge, DataTable, ErrorState, K, Section, TableSkeleton, apiGet, mono, useAdminIdentity, useFormat, useLang, useT,
  type ColumnDef, type Tone,
} from "../../_kit";
import { MarkdownView, type MarkdownLink } from "../../_kit/MarkdownView";
import { outline } from "@/lib/docsReader";
import {
  GUIDE_API, GUIDE_QUERY_KEY, cellTone, coverageRows, docView, guideLink, matrixRows, routeRows,
  stripIssueIdsMarkdown, type CapRow, type CoverageRow, type GuideDoc, type GuideResp, type MatrixRow,
  type RegisterRow, type RouteRow,
} from "@/lib/aiGatewayGuide";
import { usd } from "@/lib/aiGateway";
import { canSeePage } from "@/lib/rbac";
import { GUIDE_SOURCES, GUIDE_TAB } from "./guideTab.messages";
import { ACCESS } from "./messages";

const small = { fontSize: 12, color: K.muted } as const;
const code = { fontFamily: mono, fontSize: 12 } as const;

const TONE: Record<ReturnType<typeof cellTone>, Tone> = {
  none: "neutral", off: "neutral", ok: "success", refused: "danger", unknown: "warning",
};

/** ``howToRead``: the page's short guide (the kit GuidePanel of guide.ts / guide.vi.ts, rendered by AiGateway.tsx). */
export default function GuideTab({ howToRead }: { howToRead: ReactNode }) {
  const t = useT(GUIDE_TAB);
  const ts = useT(GUIDE_SOURCES);
  const { formatDateTime } = useFormat();
  const q = useQuery({
    queryKey: [...GUIDE_QUERY_KEY],
    queryFn: () => apiGet<GuideResp>(GUIDE_API, { admin: true }),
    staleTime: 60_000,
  });
  const data = q.data;
  return (
    <div data-testid="ai-gateway-guide" style={{ display: "grid", gap: 16, gridTemplateColumns: "minmax(0, 1fr)" }}>
      <Section title={t("howToRead")} description={t("howToReadHelp")}>
        <details>
          <summary style={{ cursor: "pointer", fontSize: 13, fontWeight: 600, color: K.ink }}>{t("howToRead")}</summary>
          <div style={{ marginTop: 10 }}>{howToRead}</div>
        </details>
      </Section>
      {q.isLoading && <TableSkeleton rows={8} cols={1} />}
      {q.error && <ErrorState message={t("couldNotLoad", (q.error as Error).message)} onRetry={() => q.refetch()} />}
      {data && <DocPart resp={data} />}
      {data && (
        <div data-testid="ai-gateway-guide-live-ready" style={{ display: "grid", gap: 16,
                                                               gridTemplateColumns: "minmax(0, 1fr)" }}>
          <div style={small}>{t("liveHelp", formatDateTime(data.generated_at))}</div>
          {data.live.errors.map((e) => (
            <div key={e.source} role="status" style={{ ...small, color: K.danger }}>
              {t("sourceError", isSource(e.source) ? ts(e.source) : e.source)}
            </div>
          ))}
          <RoutesTable resp={data} />
          <MatrixTable resp={data} />
          <RegisterTable rows={data.live.register ?? []} />
          <CapsTable rows={data.live.caps ?? []} />
          <CoverageTable rows={coverageRows(data.live.coverage)} />
        </div>
      )}
    </div>
  );
}

const GUIDE_SOURCES_KEYS = ["routes", "register", "caps", "coverage"] as const;
function isSource(source: string): source is (typeof GUIDE_SOURCES_KEYS)[number] {
  return (GUIDE_SOURCES_KEYS as readonly string[]).includes(source);
}

function DocPart({ resp }: { resp: GuideResp }) {
  const t = useT(GUIDE_TAB);
  const lang = useLang();
  const { formatDateTime } = useFormat();
  const router = useRouter();
  const role = useAdminIdentity().role;
  const view = docView(resp, lang);
  const doc = resp.doc && !resp.doc.missing ? (resp.doc as GuideDoc) : undefined;
  const canOpenDocs = canSeePage(role, "/admin/docs");
  const markdown = useMemo(() => (view.kind === "page" ? stripIssueIdsMarkdown(view.markdown) : ""), [view]);
  const headings = useMemo(() => outline(markdown), [markdown]);
  const byLine = useMemo(() => new Map(headings.map((h) => [h.line, h.slug])), [headings]);
  const slugForLine = useCallback((line: number | undefined) => (line ? byLine.get(line) : undefined), [byLine]);
  const fromPath = view.kind === "page" ? view.path : "";
  const resolveHref = useCallback((href: string | undefined): MarkdownLink => {
    const r = guideLink(href, fromPath, doc?.links, canOpenDocs);
    if (r.kind === "internal") return { kind: "internal", href: r.href, onNavigate: () => router.push(r.href) };
    return r;
  }, [fromPath, doc?.links, canOpenDocs, router]);

  let body: ReactNode;
  if (view.kind === "page") {
    body = (
      <article lang={view.lang} style={{ minWidth: 0 }}>
        {view.fallback && <div style={{ ...small, marginBottom: 10 }}>{t("langFallback")}</div>}
        <MarkdownView markdown={markdown} resolveHref={resolveHref} slugForLine={slugForLine} />
        {doc?.generated_at && (
          <div style={{ ...small, marginTop: 16 }}>{t("published", formatDateTime(doc.generated_at))}</div>
        )}
      </article>
    );
  } else {
    const text = view.kind === "restricted" ? t("docRestricted")
      : view.reason === "unavailable" ? t("docUnavailable") : t("docMissing");
    body = <div role="status" style={{ fontSize: 13, color: K.muted }}>{text}</div>;
  }
  return (
    <Section title={t("docTitle")} description={t("docHelp")} testId="ai-gateway-guide-doc">{body}</Section>
  );
}

function RoutesTable({ resp }: { resp: GuideResp }) {
  const t = useT(GUIDE_TAB);
  const routes = resp.live.routes;
  const rows = useMemo(() => routeRows(routes), [routes]);
  const columns = useMemo<ColumnDef<RouteRow, unknown>[]>(() => [
    { id: "stage", header: t("colStage"), accessorKey: "stage",
      cell: (c) => <span style={{ ...code, color: K.ink }}>{c.row.original.stage}</span> },
    { id: "role", header: t("colRole"), accessorKey: "role", cell: (c) => <span style={small}>{c.row.original.role}</span> },
    { id: "chain", header: t("colChain"), enableSorting: false,
      cell: (c) => <span style={code}>{c.row.original.chain.join(" → ")}</span> },
    { id: "first", header: t("colServesFirst"), accessorFn: (r) => r.first ?? "",
      cell: (c) => {
        const r = c.row.original;
        if (!r.active) return <Badge tone="neutral" dot={false}>{t("stageOff")}</Badge>;
        if (!r.first) return <Badge tone="danger">{t("noLiveModel")}</Badge>;
        return (
          <span style={{ display: "inline-flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
            <span style={code}>{r.first}</span>
            {r.servedByFallback && <Badge tone="warning" dot={false}>{t("servedByFallback")}</Badge>}
          </span>
        );
      } },
    { id: "skipped", header: t("colSkipped"), enableSorting: false,
      cell: (c) => <span style={small}>{c.row.original.skipped.join("; ") || "—"}</span> },
  ], [t]);
  return (
    <Section title={t("routesTitle")} padded={false}
      description={routes ? t("routesHelp", routes.version, routes.order_mode) : undefined}>
      <DataTable<RouteRow> tableId="ai-gateway-guide-routes" data={rows} columns={columns}
        getRowId={(r) => r.stage} searchable paginate={false} stickyHeader={false} tableMinWidth={860}
        emptyTitle={t("empty")} />
    </Section>
  );
}

function MatrixTable({ resp }: { resp: GuideResp }) {
  const t = useT(GUIDE_TAB);
  const ta = useT(ACCESS);
  const matrix = resp.live.matrix;
  const { rows } = useMemo(() => matrixRows(matrix), [matrix]);
  const columns = useMemo<ColumnDef<MatrixRow, unknown>[]>(() => [
    { id: "model", header: t("colModel"), accessorKey: "model_key",
      cell: (c) => (
        <span style={{ display: "grid" }}>
          <span style={{ ...code, color: K.ink }}>{c.row.original.model_key}</span>
          <span style={small}>{c.row.original.label}</span>
        </span>
      ) },
    { id: "order", header: t("colOrder"), enableSorting: false,
      cell: (c) => (
        <span style={{ display: "inline-flex", gap: 6, flexWrap: "wrap" }}>
          {c.row.original.order.map((key, i) => {
            const cell = c.row.original.cells[key];
            const tone = cellTone(cell);
            const state = cell?.access_state ?? "unknown";
            const label = tone === "off" ? t("no") : (["ok", "unknown", "access_denied", "quota_exhausted",
              "unconfigured"].includes(state) ? ta(state as "ok") : state);
            return (
              <Badge key={key} tone={TONE[tone]} dot={false}>
                <span style={code}>{i + 1}. {key}</span>&nbsp;· {label}
              </Badge>
            );
          })}
        </span>
      ) },
  ], [t, ta]);
  return (
    <Section title={t("matrixTitle")} description={t("matrixHelp")} padded={false}>
      <DataTable<MatrixRow> tableId="ai-gateway-guide-matrix" data={rows} columns={columns}
        getRowId={(r) => r.model_key} searchable paginate={false} stickyHeader={false} tableMinWidth={640}
        emptyTitle={t("empty")} />
    </Section>
  );
}

function RegisterTable({ rows }: { rows: RegisterRow[] }) {
  const t = useT(GUIDE_TAB);
  const ta = useT(ACCESS);
  const columns = useMemo<ColumnDef<RegisterRow, unknown>[]>(() => [
    { id: "connection", header: t("colConnection"), accessorKey: "connection_key",
      cell: (c) => (
        <span style={{ display: "grid" }}>
          <span style={{ ...code, color: K.ink }}>{c.row.original.connection_key}</span>
          {c.row.original.label && <span style={small}>{c.row.original.label}</span>}
        </span>
      ) },
    { id: "kind", header: t("colKind"), accessorKey: "kind", cell: (c) => <span style={small}>{c.row.original.kind}</span> },
    { id: "enabled", header: t("colEnabled"), accessorKey: "enabled",
      cell: (c) => <Badge tone={c.row.original.enabled ? "success" : "neutral"} dot={false}>
        {c.row.original.enabled ? t("yes") : t("no")}</Badge> },
    { id: "access", header: t("colAccess"), accessorKey: "access_state",
      cell: (c) => {
        const s = c.row.original.access_state ?? "unknown";
        return <span style={small}>{["ok", "unknown", "access_denied", "quota_exhausted", "unconfigured"].includes(s)
          ? ta(s as "ok") : s}</span>;
      } },
    { id: "secret", header: t("colSecret"), accessorFn: (r) => r.secret_name ?? r.credential_env ?? "",
      cell: (c) => <span style={code}>{c.row.original.secret_name ?? c.row.original.credential_env ?? "—"}</span> },
    { id: "deployments", header: t("colDeployments"), accessorKey: "deployments",
      cell: (c) => <span style={small}>{t("deploymentsOf", c.row.original.deployments_enabled,
                                          c.row.original.deployments)}</span> },
  ], [t, ta]);
  return (
    <Section title={t("registerTitle")} description={t("registerHelp")} padded={false}>
      <DataTable<RegisterRow> tableId="ai-gateway-guide-register" data={rows} columns={columns}
        getRowId={(r) => r.connection_key} searchable={false} paginate={false} stickyHeader={false}
        tableMinWidth={720} emptyTitle={t("empty")} />
    </Section>
  );
}

function CapsTable({ rows }: { rows: CapRow[] }) {
  const t = useT(GUIDE_TAB);
  const columns = useMemo<ColumnDef<CapRow, unknown>[]>(() => [
    { id: "connection", header: t("colConnection"), accessorKey: "connection_key",
      cell: (c) => <span style={{ ...code, color: K.ink }}>{c.row.original.connection_key}</span> },
    { id: "budget", header: t("colBudget"), accessorFn: (r) => r.monthly_budget_usd ?? -1, meta: { align: "right" },
      cell: (c) => <span style={code}>{c.row.original.monthly_budget_usd == null ? t("noBudget")
        : usd(c.row.original.monthly_budget_usd, 2)}</span> },
    { id: "spent", header: t("colSpent"), accessorKey: "month_spent_usd", meta: { align: "right" },
      cell: (c) => <span style={code}>{usd(c.row.original.month_spent_usd)}
        {c.row.original.used_pct != null ? ` · ${Math.round(c.row.original.used_pct)} %` : ""}</span> },
    { id: "at_cap", header: t("colAtCap"), enableSorting: false,
      cell: (c) => <span style={small}>{c.row.original.monthly_budget_usd == null ? "—"
        : `${c.row.original.hard_stop ? t("stopsAtCap") : t("alertOnly")} · ${c.row.original.alert_pct ?? "—"} %`}
      </span> },
  ], [t]);
  return (
    <Section title={t("capsTitle")} description={t("capsHelp")} padded={false}>
      <DataTable<CapRow> tableId="ai-gateway-guide-caps" data={rows} columns={columns}
        getRowId={(r) => r.connection_key} searchable={false} paginate={false} stickyHeader={false}
        tableMinWidth={560} emptyTitle={t("empty")} />
    </Section>
  );
}

function CoverageTable({ rows }: { rows: CoverageRow[] }) {
  const t = useT(GUIDE_TAB);
  const columns = useMemo<ColumnDef<CoverageRow, unknown>[]>(() => [
    { id: "stage", header: t("colStage"), accessorKey: "stage",
      cell: (c) => <span style={{ ...code, color: K.ink }}>{c.row.original.stage}</span> },
    { id: "status", header: t("colStatus"), accessorKey: "status",
      cell: (c) => {
        const s = c.row.original.status;
        if (s === "live") return <Badge tone="success">{t("statusLive")}</Badge>;
        if (s === "disabled") return <Badge tone="neutral" dot={false}>{t("statusDisabled")}</Badge>;
        return <Badge tone="danger">{t("statusNone")}</Badge>;
      } },
    { id: "live", header: t("colLiveModel"), accessorFn: (r) => r.live ?? "",
      cell: (c) => <span style={code}>{c.row.original.live ?? "—"}</span> },
  ], [t]);
  return (
    <Section title={t("coverageTitle")} description={t("coverageHelp")} padded={false}>
      <DataTable<CoverageRow> tableId="ai-gateway-guide-coverage" data={rows} columns={columns}
        getRowId={(r) => r.stage} searchable={false} paginate={false} stickyHeader={false} tableMinWidth={480}
        emptyTitle={t("empty")} />
    </Section>
  );
}
