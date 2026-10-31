"use client";
// app/admin/ai-gateway/TraceDrawer.tsx — AA-804 (UI part 1): the Trace drawer of AI providers.
//
// Every logged paid call of one job in time order, from GET /api/admin/ai-gateway/trace?job_id= (backend
// api/services/ai_gateway.py trace_job): the LLM call log, the Jev decision log, the DataForSEO call log and the
// geocoder log. Opened by the page's `?trace=job:<id>` link (the Jobs drawer's "Open trace"). Calls a provider refused
// are not logged yet (ledger v2), so the drawer says so instead of implying they did not happen. An account is named
// through lib/awsAccounts accountName (an AWS account ID or the vendor API's name), never by its key.

import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import {
  ApiError, Badge, DataTable, DateText, Drawer, Eyebrow, K, StatusBadge, apiGet, mono, serif, useT, type ColumnDef,
} from "../../_kit";
import { TRACE_SOURCES, tracePath, usd, type TraceEvent, type TraceResp } from "@/lib/aiGateway";
import { accountName } from "@/lib/awsAccounts";
import { useAccountText, useAwsAccounts } from "../_components/useAwsAccounts";
import { M, OUTCOME, SOURCE } from "./messages";

type Row = TraceEvent & { _i: number };

const small = { fontSize: 12, color: K.muted, lineHeight: 1.45 } as const;

function outcomeTone(o: string | null): "success" | "warning" | "danger" | "info" | "neutral" {
  if (o === "error" || o === "reject") return "danger";
  if (o === "grey" || o === "fallback") return "warning";
  if (o === "shadow" || o === "cache") return "info";
  if (o === "ok" || o === "accept" || o === "live") return "success";
  return "neutral";
}

function Line({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ display: "flex", gap: 10, fontSize: 12.5, padding: "3px 0", flexWrap: "wrap" }}>
      <span style={{ color: K.muted, minWidth: 110 }}>{label}</span>
      <span style={{ color: K.body, minWidth: 0, overflowWrap: "anywhere" }}>{children}</span>
    </div>
  );
}

export default function TraceDrawer({ jobId, onClose }: { jobId: string; onClose: () => void }) {
  const t = useT(M);
  const tsrc = useT(SOURCE);
  const tout = useT(OUTCOME);
  const dir = useAwsAccounts();
  const accountText = useAccountText();
  const q = useQuery({
    queryKey: ["ai-gateway", "trace", jobId],
    queryFn: () => apiGet<TraceResp>(tracePath(jobId), { admin: true }),
    retry: false,
  });
  const data = q.data;
  const rows = useMemo<Row[]>(() => (data?.events ?? []).map((e, i) => ({ ...e, _i: i })), [data]);

  const columns = useMemo<ColumnDef<Row, unknown>[]>(() => [
    { id: "at", header: t("colWhen"), accessorKey: "at",
      cell: ({ row }) => <span style={{ whiteSpace: "nowrap" }}><DateText value={row.original.at} withTime /></span> },
    { id: "source", header: t("colSource"), accessorKey: "source",
      cell: ({ row }) => <Badge dot={false}>{tsrc(row.original.source)}</Badge> },
    { id: "stage", header: t("colStage"), accessorFn: (r) => r.stage ?? "",
      cell: ({ row }) => <span style={{ fontFamily: mono, fontSize: 12 }}>{row.original.stage ?? "—"}</span> },
    { id: "what", header: t("colWhat"), accessorFn: (r) => r.what ?? "",
      cell: ({ row }) => <span style={{ fontFamily: mono, fontSize: 12 }}>{row.original.what ?? "—"}</span> },
    { id: "model", header: t("colModel"), accessorFn: (r) => r.model ?? r.provider ?? "",
      cell: ({ row: { original: e } }) => (
        <span style={{ fontFamily: mono, fontSize: 12 }}>
          {e.model ?? e.provider ?? "—"}
          {e.account ? ` · ${accountName(e.account, dir, accountText)}` : ""}
        </span>
      ) },
    { id: "outcome", header: t("colOutcome"), accessorFn: (r) => r.outcome ?? "",
      cell: ({ row: { original: e } }) => {
        const known = e.outcome != null && e.outcome in OUTCOME.en;
        const err = typeof e.detail?.error === "string" ? e.detail.error : null;
        return (
          <span style={{ display: "grid", gap: 2, justifyItems: "start" }}>
            <Badge tone={outcomeTone(e.outcome)} dot={false}>
              {known ? tout(e.outcome as keyof typeof OUTCOME.en) : (e.outcome ?? "—")}
            </Badge>
            {err && <span style={{ ...small, color: K.danger }} title={err}>{err.slice(0, 80)}</span>}
          </span>
        );
      } },
    { id: "cost", header: t("colCost"), accessorFn: (r) => r.cost_usd ?? 0, meta: { align: "right" },
      cell: ({ row }) => <span style={{ fontFamily: mono }}>{usd(row.original.cost_usd)}</span> },
    { id: "latency", header: t("colLatency"), accessorFn: (r) => r.latency_ms ?? -1, meta: { align: "right" },
      cell: ({ row }) => <span style={{ fontFamily: mono }}>
        {row.original.latency_ms == null ? "—" : `${row.original.latency_ms} ms`}</span> },
    { id: "tokens", header: t("colTokens"), accessorFn: (r) => (r.tokens_in ?? 0) + (r.tokens_out ?? 0),
      meta: { align: "right" },
      cell: ({ row: { original: e } }) => <span style={{ fontFamily: mono }}>
        {e.tokens_in == null && e.tokens_out == null ? "—" : `${e.tokens_in ?? 0} / ${e.tokens_out ?? 0}`}</span> },
  ], [t, tsrc, tout, dir, accountText]);

  const notFound = q.error instanceof ApiError && q.error.status === 404;
  const header = (
    <span style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 0, maxWidth: "100%" }}>
      <span style={{ fontFamily: serif, fontSize: 18, fontWeight: 500, color: K.ink, minWidth: 0, overflow: "hidden",
        textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={data?.job.kind}>
        {t("traceTitle")}{data ? ` · ${data.job.kind}` : ""}
      </span>
      {data && <span style={{ flexShrink: 0, display: "inline-flex" }}><StatusBadge status={data.job.status} /></span>}
    </span>
  );

  return (
    <Drawer open onClose={onClose} title={header} width={880}>
      <div data-testid="ai-gateway-trace">
        {q.isLoading && <div style={small}>{t("traceLoading")}</div>}
        {q.error && (
          <div role="alert" style={{ fontSize: 12.5, color: K.danger }}>
            {notFound ? t("traceNotFound") : t("traceLoadError", (q.error as Error).message)}
          </div>
        )}
        {data && (
          <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 14 }}
            data-testid="ai-gateway-trace-ready">
            <div>
              <Line label={t("traceJob")}>
                <span style={{ fontFamily: mono }}>{data.job.id}</span>{" · "}
                <a href={`/admin/jobs?job=${encodeURIComponent(data.job.id)}`} style={{ color: K.accent }}>
                  {t("traceOpenJob")}</a>
              </Line>
              {data.window && (
                <Line label={t("traceWindow")}>
                  <DateText value={data.window.from} withTime /> → <DateText value={data.job.finished_at} withTime />
                </Line>
              )}
              <Line label={t("colCost")}>
                <strong style={{ color: K.ink }}>{t("traceTotals", data.totals.events, usd(data.totals.cost_usd))}</strong>
              </Line>
            </div>
            <div>
              <Eyebrow>{t("traceSources")}</Eyebrow>
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                {TRACE_SOURCES.map((s) => {
                  const v = data.sources[s];
                  return (
                    <Badge key={s} tone={v.error ? "danger" : v.events ? "info" : "neutral"} dot={false}>
                      {tsrc(s)}: {v.read ? t("traceSourceLine", v.events, usd(v.cost_usd)) : t("traceNotReadSource")}
                    </Badge>
                  );
                })}
              </div>
              {TRACE_SOURCES.filter((s) => data.sources[s].error).map((s) => (
                <div key={s} style={{ ...small, color: K.danger, marginTop: 4 }}>{data.sources[s].error}</div>
              ))}
            </div>
            <div style={{ display: "grid", gap: 4 }}>
              {data.notes.includes("job_not_started") && <div style={small}>{t("noteNotStarted")}</div>}
              {data.notes.includes("truncated") && <div style={small}>{t("noteTruncated", data.limit_per_source)}</div>}
              {data.notes.includes("refused_calls_not_logged") && <div style={small}>{t("noteRefused")}</div>}
            </div>
            <DataTable<Row>
              tableId="ai-gateway-trace-events" data={rows} columns={columns} getRowId={(r) => String(r._i)}
              searchable={false} paginate={false} tableMinWidth={900} maxBodyHeight={520}
              emptyTitle={t("traceEmpty")}
            />
          </div>
        )}
      </div>
    </Drawer>
  );
}
