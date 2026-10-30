"use client";
// app/admin/platform/cost/CostByApp.tsx
// AA-884 (S241 admin console GĐ10) — Platform › AI & spend › Cost per app: what each app cost in one UTC
// month (month to date for the current one), AWS and AI together, plus LLM spend by app.
//
// API: GET /api/admin/platform/cost?month=YYYY-MM (backend api/routers/admin_platform_ops.py →
// services/platform_cost.py cost_by_app(), the console's ONE cost module — AA-858's overview card reads the
// same function, so the two pages agree; spend.read = admin; BFF API_READ_RULES "platform").
//   - AWS = acc2 Cost Explorer grouped by the App cost-allocation tag (stored snapshot). Untagged spend is its
//     own row ("Not assigned to an app") with a per-service breakdown. Shared tags are their own rows, never split.
//   - AI = the LLM gateway's estimates by app (quality_signal.app). The part billed on acc2 (Bedrock there) is
//     already inside AWS, so Total = AWS + AI − "AI on the acc2 bill" = AWS + "AI outside the AWS bill".
// S243 — one presentation with the Overview card: backend row labels (platform_cost ROW_LABELS), the same columns
// (lib/costWording), a KPI strip that adds up on screen (AWS + AI estimate − AI already on the bill = Total),
// right-aligned numeric headers (kit DataTable meta.align), and data sources in a collapsed "Data sources" block.
//   - The GET may refresh a stale month itself (one CE request, ~$0.01, at most about once a day per window,
//     behind a Redis lock); "Refresh from AWS" is the explicit POST /api/admin/cost-explorer/check (spend.write,
//     admin; the same call as Cost Operations, plus the App-tag request) for this month's window.
// No polling: one load per month viewed.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, ChevronRight, CloudDownload, Info } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";
import {
  Badge,
  Button,
  DataTable,
  Drawer,
  ErrorState,
  K,
  PageHeader,
  RADIUS,
  SkeletonBar,
  SkeletonStyle,
  apiGet,
  apiSend,
  mono,
  sans,
  useFormat,
  useLang,
  useT,
  type ColumnDef,
  type Tone, PageFrame,
} from "../../../_kit";
import { shownSum } from "@/lib/costWording";
import { COST_WORDING } from "@/lib/costWording.messages";
import type { Lang } from "../../../_kit/core/i18n";
import {
  currentUtcMonth,
  lastIncludedDay,
  usd,
  type CeCheckResp,
  type CostResp,
  type CostRow,
  type CostRowKind,
  type SatelliteLine,
} from "@/lib/platform";
import { COST } from "./messages";

type KindWord = "kindApp" | "kindLlm" | "kindShared" | "kindOther" | "kindUntagged";
const KIND_TONE: Record<CostRowKind, { tone: Tone; label: KindWord }> = {
  app: { tone: "accent", label: "kindApp" },
  llm_app: { tone: "purple", label: "kindLlm" },
  shared_tag: { tone: "info", label: "kindShared" },
  other_tag: { tone: "neutral", label: "kindOther" },
  untagged: { tone: "warning", label: "kindUntagged" },
};

const MONTH_RE = /^\d{4}-\d{2}$/;

function monthLabel(month: string, lang: Lang = "en"): string {
  const d = new Date(`${month}-01T00:00:00Z`);
  return Number.isNaN(d.getTime())
    ? month
    : d.toLocaleString(lang === "vi" ? "vi-VN" : "en-US", { month: "long", year: "numeric", timeZone: "UTC" });
}

export default function CostByApp() {
  const t = useT(COST);
  const qc = useQueryClient();
  const [thisMonth] = useState(() => currentUtcMonth());
  const [month, setMonth] = useState(thisMonth);
  const key = ["platform-cost", month] as const;

  const cost = useQuery({
    queryKey: key,
    queryFn: () => apiGet<CostResp>(`/api/admin/platform/cost?month=${encodeURIComponent(month)}`),
    staleTime: 60_000,
    retry: false,
  });
  const data = cost.data;

  const check = useMutation({
    mutationFn: (win: { start: string; end: string }) =>
      apiSend<CeCheckResp>(
        `/api/admin/cost-explorer/check?start=${win.start}&end=${win.end}`,
        { method: "POST" },
      ),
    onSettled: () => qc.invalidateQueries({ queryKey: ["platform-cost", month] }),
  });
  const refreshFromAws = () => {
    if (!data) return;
    check.mutate({ start: data.period_start, end: lastIncludedDay(data.period_end_exclusive) });
  };

  return (
    <PageFrame>
      <SkeletonStyle />
      <PageHeader
        title={t("title")}
        description={
          <>
            {t("description")}{" "}
            <Link href="/admin/cost-ops" style={{ color: K.accentDeep, fontWeight: 600 }}>{t("costOps")}</Link>.
          </>
        }
        actions={
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
            <label style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12, color: K.muted, fontFamily: sans }}>
              {t("month")}
              <input
                type="month"
                value={month}
                max={thisMonth}
                onChange={(e) => { if (MONTH_RE.test(e.target.value) && e.target.value <= thisMonth) setMonth(e.target.value); }}
                aria-label={t("monthAria")}
                style={{
                  padding: "5px 8px", borderRadius: RADIUS.md, border: `1px solid ${K.line}`, background: K.card,
                  color: K.ink, fontSize: 13, fontFamily: sans, maxWidth: "100%", minWidth: 0,
                }}
              />
            </label>
            <Button variant="secondary" size="sm" onClick={refreshFromAws} disabled={!data || check.isPending}
              title={t("refreshTitle")}>
              <CloudDownload size={13} /> {check.isPending ? t("fetching") : t("refreshAws")}
            </Button>
          </div>
        }
      />

      {check.isError && (
        <div style={{ ...noteStyle, color: K.danger, marginBottom: 12 }}>
          {t("refreshFailed", (check.error as Error)?.message || t("unknownError"))}
        </div>
      )}
      {check.data && <CheckOutcome r={check.data} />}

      {cost.isError ? (
        <ErrorState message={(cost.error as Error)?.message || t("loadError")} onRetry={() => cost.refetch()} />
      ) : !data ? (
        <CostSkeleton />
      ) : (
        <div data-testid="platform-cost-ready" style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <Summary data={data} />
          <DataStatus data={data} />
          <CostTable data={data} />
          <LlmByApp data={data} />
          <DataSources sources={data.sources} />
        </div>
      )}
    </PageFrame>
  );
}

// ── shared bits ─────────────────────────────────────────────────────────────────────────────

const noteStyle: React.CSSProperties = { fontSize: 12, color: K.muted, fontFamily: sans, lineHeight: 1.5 };

const panelStyle: React.CSSProperties = {
  background: K.card, border: `1px solid ${K.line}`, borderRadius: RADIUS.lg, padding: "12px 14px",
  fontFamily: sans, fontSize: 13, color: K.ink, minWidth: 0,
};

function CostSkeleton() {
  return (
    <div style={{ ...panelStyle, display: "flex", flexDirection: "column", gap: 10 }}>
      <SkeletonBar w="40%" h={16} />
      <SkeletonBar />
      <SkeletonBar />
      <SkeletonBar w="85%" />
    </div>
  );
}

function CheckOutcome({ r }: { r: CeCheckResp }) {
  const t = useT(COST);
  const { formatDate } = useFormat();
  const errs = Object.entries(r.errors ?? {});
  return (
    <div style={{ ...noteStyle, marginBottom: 12, color: errs.length ? K.amber : K.success }}>
      {t("fetched", formatDate(r.period_start), formatDate(lastIncludedDay(r.period_end_exclusive)), r.row_count,
        r.app_tag_row_count ?? 0)}
      {errs.length > 0 && t("errors", errs.map(([k, v]) => `${k}: ${v}`).join("; "))}
    </div>
  );
}

function Stat({ label, value, title, testId, op, strong }: {
  label: string; value: string; title?: string; testId?: string; op?: string; strong?: boolean;
}) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}>
      {op && <span aria-hidden style={{ fontSize: 18, fontWeight: 600, color: K.muted, width: 12, textAlign: "center" }}>{op}</span>}
      <div data-testid={testId} title={title} style={{
        ...panelStyle, display: "flex", flexDirection: "column", gap: 2, padding: "9px 12px", flex: 1,
        background: strong ? K.bg : K.card,
      }}>
        <span style={{ fontSize: 12, color: K.muted, display: "inline-flex", alignItems: "center", gap: 4 }}>
          {label} {title && <Info size={11} />}
        </span>
        <span data-testid={testId ? `${testId}-value` : undefined}
          style={{ fontSize: 18, fontWeight: 600, fontVariantNumeric: "tabular-nums" }}>{value}</span>
      </div>
    </div>
  );
}

/** The KPI strip adds up on screen: AWS bill + AI estimate − AI already on the AWS bill = Total. */
function Summary({ data }: { data: CostResp }) {
  const t = useT(COST);
  const cw = useT(COST_WORDING);
  const lang = useLang();
  const aws = data.aws.total_usd;
  const untaggedPct = aws ? Math.round((100 * data.aws.untagged_usd) / aws) : null;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <div data-testid="platform-cost-kpis" style={{
        display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 170px), 1fr))", gap: 10,
      }}>
        <Stat label={cw("awsBill")} value={aws == null ? "—" : usd(shownSum(aws))} title={cw("awsBillSrc")} testId="platform-cost-aws" />
        <Stat op="+" label={cw("aiEstimate")} value={usd(shownSum(data.totals.ai_usd))}
          title={t("aiEstimateTitle")} testId="platform-cost-ai" />
        <Stat op="−" label={cw("aiOnBill")} value={usd(shownSum(data.totals.ai_in_aws_acc2_usd))} title={cw("aiOnBillSrc")}
          testId="platform-cost-ai-on-bill" />
        <Stat op="=" strong label={t("total", monthLabel(data.month, lang))}
          value={usd(shownSum(data.aws.total_usd, data.totals.ai_usd, -(data.totals.ai_in_aws_acc2_usd ?? 0)))}
          testId="platform-cost-total" />
      </div>
      {untaggedPct != null && (
        <div style={noteStyle} title={t("untaggedTitle")}>
          {t("untagged", usd(data.aws.untagged_usd), untaggedPct)}
        </div>
      )}
    </div>
  );
}

function DataStatus({ data }: { data: CostResp }) {
  const t = useT(COST);
  const lang = useLang();
  const { formatDate, formatDateTime } = useFormat();
  const r = data.refresh;
  let refreshLine: React.ReactNode = null;
  if (r.attempted && r.ok) {
    refreshLine = <span style={{ color: K.success }}>{t("ceFetched", r.ce_requests ?? 1, usd(r.est_cost_usd ?? 0.01))}</span>;
  } else if (r.attempted && r.ok === false) {
    refreshLine = <span style={{ color: K.danger }}>{t("autoFailed", r.error ?? t("unknownError"))}</span>;
  } else if (r.stale) {
    refreshLine = <span style={{ color: K.amber }}>{t("stale", r.stale_reason ?? r.reason ?? "")} {r.skipped ? "" : t("useRefresh")}</span>;
  }
  return (
    <div style={{ ...noteStyle, display: "flex", flexDirection: "column", gap: 2 }}>
      {data.aws.has_data ? (
        <span>
          {t("covers", formatDate(data.aws.covered_from), formatDate(data.aws.covered_to), formatDateTime(data.aws.fetched_at))}
        </span>
      ) : (
        <span style={{ color: K.amber }}>{t("noAws", monthLabel(data.month, lang))}</span>
      )}
      {refreshLine}
      {data.crm_llm && data.crm_llm.status !== "ok" && data.crm_llm.status !== "not_requested" && (
        <span style={{ color: K.amber }} data-testid="cost-crm-llm-missing">
          {t("crmMissing", data.crm_llm.reason ?? data.crm_llm.status)}
        </span>
      )}
    </div>
  );
}

// ── the table ───────────────────────────────────────────────────────────────────────────────
// Kit DataTable (sort, CSV); a row with a breakdown (AWS services, AI by provider / account) opens it in a
// Drawer — the Untagged row's per-service list is the one people need most (AA-828).

const num: React.CSSProperties = { display: "block", textAlign: "right", fontVariantNumeric: "tabular-nums", whiteSpace: "nowrap" };

function hasBreakdown(r: CostRow): boolean {
  return r.services.length > 0 || r.ai_by_source.length > 0;
}

function CostTable({ data }: { data: CostResp }) {
  const t = useT(COST);
  const cw = useT(COST_WORDING);
  const lang = useLang();
  const [openApp, setOpenApp] = useState<string | null>(null);
  const open = data.apps.find((r) => r.app === openApp) ?? null;
  const columns = useMemo<ColumnDef<CostRow, unknown>[]>(() => [
    {
      id: "app",
      accessorFn: (r) => r.label,
      header: t("colApp"),
      cell: (c) => {
        const r = c.row.original;
        return (
          <span style={{ display: "flex", flexDirection: "column", gap: 3, minWidth: 0 }}>
            <span style={{ display: "inline-flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
              <span style={{ fontWeight: 600 }}>{r.label}</span>
              {r.kind !== "app" && <Badge tone={KIND_TONE[r.kind].tone} dot={false}>{t(KIND_TONE[r.kind].label)}</Badge>}
            </span>
            {r.hint && <span style={{ ...noteStyle, whiteSpace: "normal" }}>{r.hint}</span>}
            {hasBreakdown(r) && (
              <span style={{ fontSize: 11.5, color: K.accentDeep, display: "inline-flex", alignItems: "center", gap: 3 }}>
                {t("breakdown")} <ChevronRight size={11} />
              </span>
            )}
          </span>
        );
      },
    },
    {
      accessorKey: "aws_usd",
      header: cw("awsBill"),
      meta: { align: "right" },
      cell: (c) => <span style={num} title={cw("awsBillSrc")}>{usd(shownSum(c.row.original.aws_usd))}</span>,
    },
    {
      accessorKey: "ai_outside_aws_usd",
      header: cw("aiOutside"),
      meta: { align: "right" },
      cell: (c) => {
        const r = c.row.original;
        return (
          <span style={{ ...num, color: r.ai_outside_aws_usd ? K.body : K.muted }}
            title={r.ai_in_aws_acc2_usd
              ? t("outsideTitle", cw("aiEstimate"), usd(r.ai_usd), usd(r.ai_in_aws_acc2_usd))
              : cw("aiOutsideSrc")}>
            {usd(shownSum(r.ai_usd, -r.ai_in_aws_acc2_usd))}
          </span>
        );
      },
    },
    {
      accessorKey: "total_usd",
      header: t("colTotal"),
      meta: { align: "right" },
      cell: (c) => {
        const r = c.row.original;
        return (
          <span style={{ ...num, fontWeight: 600 }} title={`${cw("awsBill")} + ${cw("aiOutside").toLowerCase()}`}>
            {usd(shownSum(r.aws_usd, r.ai_usd, -r.ai_in_aws_acc2_usd))}
          </span>
        );
      },
    },
  ], [t, cw]);

  return (
    <section style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <div style={{ fontWeight: 600, fontSize: 14, fontFamily: sans }}>{t("byApp")}</div>
      <DataTable<CostRow>
        tableId="platform-cost"
        data={data.apps}
        columns={columns}
        getRowId={(r) => r.app}
        pageSize={50}
        enableCsv
        csvFilename={`cis-cost-per-app-${data.month}.csv`}
        emptyTitle={t("noCost")}
        emptyDescription={t("nothingFor", monthLabel(data.month, lang))}
        onRowClick={(r) => setOpenApp(hasBreakdown(r) ? r.app : null)}
        tableMinWidth={640}
      />
      <div data-testid="platform-cost-totals" style={{
        ...panelStyle, display: "flex", flexWrap: "wrap", gap: "4px 18px", justifyContent: "flex-end", fontSize: 12.5,
      }}>
        <span><span style={{ color: K.muted }}>{cw("awsBill")}</span> <strong>{data.totals.aws_usd == null ? "—" : usd(shownSum(data.totals.aws_usd))}</strong></span>
        <span><span style={{ color: K.muted }}>+ {cw("aiOutside")}</span> <strong>{usd(shownSum(data.totals.ai_usd, -(data.totals.ai_in_aws_acc2_usd ?? 0)))}</strong></span>
        <span><span style={{ color: K.muted }}>{t("totalEq")}</span> <strong>{usd(shownSum(data.totals.aws_usd, data.totals.ai_usd, -(data.totals.ai_in_aws_acc2_usd ?? 0)))}</strong></span>
      </div>
      <Drawer open={!!open} onClose={() => setOpenApp(null)} title={open ? `${open.label} · ${monthLabel(data.month, lang)}` : t("breakdown")}>
        {open && <Breakdown r={open} />}
      </Drawer>
    </section>
  );
}

function Breakdown({ r }: { r: CostRow }) {
  const t = useT(COST);
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 260px), 1fr))", gap: 16 }}>
      {r.services.length > 0 && (
        <div>
          <div style={{ ...noteStyle, marginBottom: 4 }}>
            {t("awsServices")}{r.aws_tags.length > 0 && r.aws_tags.some(Boolean) ? t("tag", r.aws_tags.filter(Boolean).join(", ")) : ""}
          </div>
          <ul style={listStyle}>
            {r.services.map((s, i) => (
              <li key={`${s.service}-${i}`} style={lineStyle}>
                <span style={{ minWidth: 0, overflowWrap: "anywhere" }}>
                  {s.service} {s.is_bedrock && <Badge tone="purple" dot={false}>Bedrock</Badge>}
                </span>
                <span style={num}>{usd(s.amount_usd)}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {r.ai_by_source.length > 0 && (
        <div>
          <div style={{ ...noteStyle, marginBottom: 4 }}>{t("aiBySource", r.ai_calls.toLocaleString("en-US"))}</div>
          <ul style={listStyle}>
            {r.ai_by_source.map((s, i) => (
              <li key={`${s.provider}-${s.account}-${i}`} style={lineStyle}>
                <span style={{ minWidth: 0, overflowWrap: "anywhere" }}>
                  <span style={{ fontFamily: mono, fontSize: 12 }}>{s.provider ?? t("unknown")}</span>
                  {s.account_name && <span style={{ color: K.muted, fontFamily: mono }}> · {s.account_name}</span>}
                  <span style={{ color: K.muted }}>{t("calls", s.calls.toLocaleString("en-US"))}</span>
                  {s.billed_in_aws_acc2 && <span style={{ color: K.muted }} title={t("onBillTitle")}>{t("onBill")}</span>}
                </span>
                <span style={num}>{usd(s.cost_usd)}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

const listStyle: React.CSSProperties = { margin: 0, padding: 0, listStyle: "none", display: "flex", flexDirection: "column", gap: 4, fontSize: 12.5 };
const lineStyle: React.CSSProperties = { display: "flex", flexWrap: "wrap", justifyContent: "space-between", gap: "2px 12px", alignItems: "baseline" };

// ── LLM spend by app ────────────────────────────────────────────────────────────────────────

function LlmByApp({ data }: { data: CostResp }) {
  const t = useT(COST);
  const lang = useLang();
  const { formatDateTime } = useFormat();
  const rows = data.apps.filter((r) => r.ai_usd > 0 || r.ai_calls > 0).sort((a, b) => b.ai_usd - a.ai_usd);
  const max = Math.max(0, ...rows.map((r) => r.ai_usd));
  const sats: SatelliteLine[] = data.llm_satellites.lines ?? [];
  return (
    <section data-testid="platform-cost-llm" style={{ ...panelStyle, display: "flex", flexDirection: "column", gap: 10 }}>
      <div style={{ fontWeight: 600, fontSize: 14 }}>{t("aiByApp")}</div>
      <div style={noteStyle}>{t("aiByAppNote")}</div>
      {rows.length === 0 ? (
        <div style={noteStyle}>{t("noLlm", monthLabel(data.month, lang))}</div>
      ) : (
        <ul style={{ ...listStyle, gap: 8, fontSize: 13 }}>
          {rows.map((r) => (
            <li key={r.app} style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <div style={lineStyle}>
                <span style={{ minWidth: 0, overflowWrap: "anywhere" }}>
                  <span style={{ fontWeight: 600 }}>{r.label}</span>
                  <span style={{ color: K.muted, fontSize: 12 }}>{t("calls", r.ai_calls.toLocaleString("en-US"))}</span>
                </span>
                <span style={{ ...num, fontWeight: 600 }}>{usd(r.ai_usd)}</span>
              </div>
              <div aria-hidden style={{ height: 6, borderRadius: RADIUS.pill, background: K.line2, overflow: "hidden" }}>
                <div style={{ width: `${max ? Math.max(1, (100 * r.ai_usd) / max) : 0}%`, height: "100%", background: K.accent }} />
              </div>
            </li>
          ))}
        </ul>
      )}
      {sats.length > 0 && (
        <div style={{ borderTop: `1px solid ${K.line}`, paddingTop: 10, display: "flex", flexDirection: "column", gap: 4 }}>
          <div style={{ ...noteStyle, display: "inline-flex", alignItems: "center", gap: 4 }} title={data.sources.llm_satellites}>
            {t("otherAccounts")} <Info size={11} />
          </div>
          {sats.map((s, i) => (
            <div key={s.key ?? i} style={{ ...lineStyle, fontSize: 12.5 }}>
              <span style={{ fontFamily: mono }}>{s.label ?? s.account_id ?? t("awsAccount")}</span>
              <span style={num}>
                {t("satLine", s.ce_bedrock_usd == null ? t("notFetched") : usd(s.ce_bedrock_usd), usd(s.llm_log_estimate_usd))}
              </span>
            </div>
          ))}
          {data.llm_satellites.ce_fetched_at && (
            <div style={noteStyle}>{t("ceFrom", formatDateTime(data.llm_satellites.ce_fetched_at))}</div>
          )}
        </div>
      )}
      <Link href="/admin/cost-ops" style={{ color: K.accentDeep, fontWeight: 600, fontSize: 12.5, textDecoration: "none", display: "inline-flex", alignItems: "center", gap: 4 }}>
        {t("perProvider")} <ArrowRight size={11} />
      </Link>
    </section>
  );
}

// ── data sources (collapsed) ────────────────────────────────────────────────────────────────

/** Where each number comes from, for whoever needs the exact tables — out of the way of the plain copy (S243). */
function DataSources({ sources }: { sources: Record<string, string> }) {
  const t = useT(COST);
  const cw = useT(COST_WORDING);
  const SOURCE_LABEL: Record<string, string> = {
    aws_usd: cw("awsBill"),
    ai_usd: cw("aiEstimate"),
    ai_outside_aws_usd: cw("aiOutside"),
    total_usd: t("colTotal"),
    llm_satellites: t("otherAiAccounts"),
  };
  return (
    <details data-testid="platform-cost-sources" style={{ ...noteStyle }}>
      <summary style={{ cursor: "pointer", fontWeight: 600 }}>{t("dataSources")}</summary>
      <ul style={{ margin: "6px 0 0", paddingLeft: 18, display: "flex", flexDirection: "column", gap: 2 }}>
        {Object.entries(sources).map(([k, v]) => (
          <li key={k} style={{ overflowWrap: "anywhere" }}>
            <strong>{SOURCE_LABEL[k] ?? k}</strong>: <span style={{ fontFamily: mono, fontSize: 11.5 }}>{v}</span>
          </li>
        ))}
      </ul>
    </details>
  );
}
