"use client";
// app/admin/jobs/JobDrawer.tsx — AA-721 job detail in the kit Drawer, data via react-query.
// Lifecycle (attempts, releases, lease), cost split (LLM from llm_call_log vs what the handler
// reported), the job's LLM calls, progress/ETA, and the domain rows the job wrote (tenant version /
// content piece / tour deep links). Payload/result come from here (`/jobs/{id}`), not the list
// (S241: the list asks for fields=summary). Polls on the list's cadence only while the job is
// queued/running; a finished job is fetched once (S241, Vercel Fluid CPU). The LLM-calls query
// carries the job's active flag in its key, so the running→finished transition triggers one last
// fetch of the calls and then it stops too.

import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import {
  Button, DataTable, Drawer, StatusBadge, apiGet, useFormat, useT, type ColumnDef, Eyebrow, K, mono, serif,
} from "../../_kit";
import { TenantName } from "../_components/TenantSelect";
import { useAccountText, useAwsAccounts } from "../_components/useAwsAccounts";
import { tenant360Href } from "@/lib/adminIa";
import { traceHref } from "@/lib/aiGateway";
import { accountName, withAccountNames } from "@/lib/awsAccounts";
import {
  canCancel, fmtSeconds, isJevHeld, jevBlock, releases, runSeconds, stepProgress, usd, type Job, type JobAction, type JevBlock,
} from "./jobsShared";
import { JOBS } from "./messages";

interface LlmCall {
  created_at: string; stage: string; role: string; model: string; account: string | null;
  provider: string | null; fallback_used: boolean | null; tokens_in: number | null;
  tokens_out: number | null; cost_usd: number | null; texts: number | null;
}
type StageRow = LlmCalls["by_stage"][number];
type CallRow = LlmCall & { _i: number };
interface JevStageRow { stage: string; estCalls: number | null; estUsd: number | null; calls: number | null; usd: number | null }
interface LlmCalls {
  calls: LlmCall[];
  totals: { calls: number; cost_usd: number; tokens_in: number; tokens_out: number; first_at: string | null; last_at: string | null };
  by_stage: { stage: string; model: string; calls: number; cost_usd: number }[];
  truncated: boolean;
}

// AA-919 S254: a tenant is printed by name (full id on hover) and links to its Tenant 360 (a route since AA-1039 R6 rest).
const tenantHref = (id: string) => tenant360Href(id);

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ display: "flex", gap: 12, fontSize: 12.5, padding: "4px 0", borderBottom: `1px solid ${K.line2}` }}>
      <div style={{ width: 130, flexShrink: 0, color: K.muted }}>{label}</div>
      <div style={{ minWidth: 0, color: K.body, wordBreak: "break-word" }}>{children}</div>
    </div>
  );
}

function Json({ label, value }: { label: string; value: unknown }) {
  if (value == null || (typeof value === "object" && Object.keys(value as object).length === 0)) return null;
  return (
    <div style={{ marginTop: 12 }}>
      <Eyebrow>{label}</Eyebrow>
      <pre style={{ margin: 0, fontFamily: mono, fontSize: 11, color: K.ink3, background: K.bg, border: `1px solid ${K.line}`, borderRadius: 6, padding: 10, maxHeight: 220, overflow: "auto", whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
        {JSON.stringify(value, null, 2)}
      </pre>
    </div>
  );
}

// AA-1083 — a recompute's Jev side: who it asked about, the estimate before the run, the cap, the real spend, and a
// release record. Read from `result.jev` (services/recompute/jev_scope.py).
function JevSection({ jev }: { jev: JevBlock }) {
  const t = useT(JOBS);
  const { formatDateTime: fmtTime } = useFormat();
  const est = jev.estimate;
  const spend = jev.spend;
  const rows = useMemo<JevStageRow[]>(() => {
    const stages = new Set([...Object.keys(est?.by_stage ?? {}), ...Object.keys(spend?.by_stage ?? {})]);
    return [...stages].sort().map(stage => ({
      stage,
      estCalls: est?.by_stage?.[stage]?.calls ?? null, estUsd: est?.by_stage?.[stage]?.usd ?? null,
      calls: spend?.by_stage?.[stage]?.calls ?? null, usd: spend?.by_stage?.[stage]?.usd ?? null,
    }));
  }, [est, spend]);
  const columns = useMemo<ColumnDef<JevStageRow, unknown>[]>(() => [
    { id: "stage", header: t("stage"), accessorKey: "stage", cell: ({ row }) => <span style={{ fontFamily: mono }}>{row.original.stage}</span> },
    {
      id: "est", header: t("jevEstCol"), accessorKey: "estCalls", meta: { align: "right" },
      cell: ({ row: { original: r } }) => <span style={{ fontFamily: mono }}>{r.estCalls == null ? "—" : `${r.estCalls} · ${usd(r.estUsd, 4)}`}</span>,
    },
    {
      id: "spent", header: t("jevSpentCol"), accessorKey: "calls", meta: { align: "right" },
      cell: ({ row: { original: r } }) => <span style={{ fontFamily: mono }}>{r.calls == null ? "—" : `${r.calls} · ${usd(r.usd, 4)}`}</span>,
    },
  ], [t]);
  const capText = jev.cap_usd == null ? t("jevCapNone") : `${usd(jev.cap_usd, 2)}${jev.hard_stop === false ? t("jevCapAlert") : ""}`;
  return (
    <div style={{ marginTop: 16 }} data-testid="job-drawer-jev">
      <Eyebrow>{t("jevTitle")}</Eyebrow>
      <Row label={t("jevScope")}>{jev.scope === "all" ? t("jevScopeAll") : t("jevScopeChanged", est?.segments ?? null)}</Row>
      <Row label={t("jevEstimate")}>
        {jev.estimate_error ? t("jevEstimateFailed", jev.estimate_error)
          : est ? <span style={{ fontFamily: mono }}>{t("jevEstimateLine", est.new_calls, usd(est.usd, 4))}</span> : "—"}
      </Row>
      <Row label={t("jevCap")}><span style={{ fontFamily: mono }}>{capText}</span></Row>
      {jev.released && (
        <Row label={t("jevReleased")}>{t("jevReleasedLine", jev.released.by ?? "—", fmtTime(jev.released.at ?? null))}</Row>
      )}
      {spend && !spend.error && (
        <Row label={t("jevSpent")}>
          <span style={{ fontFamily: mono }}>{t("jevSpentLine", spend.calls, usd(spend.usd, 4), spend.refused)}</span>
        </Row>
      )}
      {rows.length > 0 && (
        <div style={{ marginTop: 8 }}>
          <Eyebrow>{t("jevByStage")}</Eyebrow>
          <DataTable<JevStageRow>
            data={rows} columns={columns} tableId="job-drawer-jev-stages"
            getRowId={r => r.stage} searchable={false} paginate={false} stickyHeader={false}
          />
        </div>
      )}
    </div>
  );
}

export default function JobDrawer({ jobId, refetchMs, expectedSeconds, maxReleases, eta, onClose, onAct, busy }: {
  jobId: string;
  refetchMs: number;
  expectedSeconds?: number;
  maxReleases: number;
  eta: string | null;
  onClose: () => void;
  onAct: (job: Job, action: JobAction) => void;
  busy: boolean;
}) {
  const t = useT(JOBS);
  const { formatDateTime: fmtTime } = useFormat();
  const awsAccounts = useAwsAccounts();
  const accountText = useAccountText();
  const { data: job, isError, error } = useQuery({
    queryKey: ["job", jobId],
    queryFn: () => apiGet<Job>(`/api/admin/job-runner/jobs/${jobId}`, { admin: true }),
    // Before the first response (no data) keep polling so a transient error still retries.
    refetchInterval: (query) => {
      const st = query.state.data?.status;
      return st === undefined || st === "queued" || st === "running" ? refetchMs : false;
    },
    refetchIntervalInBackground: false,
    placeholderData: (prev) => prev,
  });
  const active = job ? job.status === "queued" || job.status === "running" : false;
  const { data: llm } = useQuery({
    queryKey: ["job-llm", jobId, active],
    queryFn: () => apiGet<LlmCalls>(`/api/admin/job-runner/jobs/${jobId}/llm-calls?limit=200`, { admin: true }),
    enabled: !!job,
    refetchInterval: active ? refetchMs : false,
    refetchIntervalInBackground: false,
    placeholderData: (prev) => prev,
  });

  const secs = job ? runSeconds(job) : null;
  const rel = job ? releases(job) : 0;
  const prog = job ? stepProgress(job) : null;
  const llmCost = llm?.totals.cost_usd ?? job?.llm_cost_usd ?? 0;
  const total = job?.cost_usd ?? 0;
  const links = job?.links;
  const jev = job ? jevBlock(job) : null;   // AA-1083: recompute only

  // AA-994 S6b: the two LLM tables are kit DataTables (were raw tables with the legacy TH / TD styles).
  const stageColumns = useMemo<ColumnDef<StageRow, unknown>[]>(() => [
    { id: "stage", header: t("stage"), accessorKey: "stage", cell: ({ row }) => <span style={{ fontFamily: mono }}>{row.original.stage}</span> },
    { id: "model", header: t("model"), accessorKey: "model", cell: ({ row }) => <span style={{ fontFamily: mono }}>{row.original.model}</span> },
    { id: "calls", header: t("callsCol"), accessorKey: "calls", meta: { align: "right" }, cell: ({ row }) => <span style={{ fontFamily: mono }}>{row.original.calls}</span> },
    { id: "cost", header: t("cost"), accessorKey: "cost_usd", meta: { align: "right" }, cell: ({ row }) => <span style={{ fontFamily: mono }}>{usd(row.original.cost_usd, 5)}</span> },
  ], [t]);
  const callRows = useMemo<CallRow[]>(() => (llm?.calls ?? []).map((c, i) => ({ ...c, _i: i })), [llm]);
  const callColumns = useMemo<ColumnDef<CallRow, unknown>[]>(() => [
    { id: "time", header: t("time"), accessorKey: "created_at", cell: ({ row }) => <span style={{ whiteSpace: "nowrap" }}>{fmtTime(row.original.created_at)}</span> },
    {
      id: "stage", header: t("stage"), accessorKey: "stage",
      cell: ({ row: { original: c } }) => <span style={{ fontFamily: mono }}>{c.stage}{c.texts && c.texts > 1 ? ` ×${c.texts}` : ""}</span>,
    },
    {
      id: "model", header: t("modelAccount"), accessorKey: "model",
      cell: ({ row: { original: c } }) => (
        <span style={{ fontFamily: mono }}>
          {c.model}{c.account ? ` · ${accountName(c.account, awsAccounts, accountText)}` : ""}{c.fallback_used ? t("fallback") : ""}
        </span>
      ),
    },
    {
      id: "tokens", header: t("tokens"), accessorFn: (c) => (c.tokens_in ?? 0) + (c.tokens_out ?? 0), meta: { align: "right" },
      cell: ({ row: { original: c } }) => <span style={{ fontFamily: mono }}>{c.tokens_in ?? 0}/{c.tokens_out ?? 0}</span>,
    },
    { id: "cost", header: t("cost"), accessorKey: "cost_usd", meta: { align: "right" }, cell: ({ row }) => <span style={{ fontFamily: mono }}>{usd(row.original.cost_usd, 6)}</span> },
  ], [t, fmtTime, awsAccounts, accountText]);

  const header = (
    // S241 UI verify — one row that fits the Drawer's title box: the kind ends in an ellipsis and the
    // status badge keeps its size (it used to push the close X off-screen at 390).
    <span style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 0, maxWidth: "100%" }}>
      <span title={job?.kind ?? t("job")} style={{ fontFamily: serif, fontSize: 18, fontWeight: 500, color: K.ink, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{job?.kind ?? t("job")}</span>
      {job && <span style={{ flexShrink: 0, display: "inline-flex" }}><StatusBadge status={job.status} /></span>}
    </span>
  );

  const footer = job && (
    <>
      {canCancel(job) && (
        <Button size="sm" variant="danger" disabled={busy} onClick={() => onAct(job, "cancel")}>{t("cancel")}</Button>
      )}
      {(job.status === "failed" || job.status === "stopped_budget" || job.status === "cancelled") && (
        <Button size="sm" disabled={busy} onClick={() => onAct(job, "retry")}>{t("retry")}</Button>
      )}
      {isJevHeld(job) && (
        <Button size="sm" variant="primary" disabled={busy} title={t("releaseTitle")} testId="job-drawer-release-jev"
          onClick={() => onAct(job, "release-jev-cap")}>{t("release")}</Button>
      )}
    </>
  );

  return (
    <Drawer open onClose={onClose} title={header} footer={footer || undefined} width={760}>
      {isError && <div style={{ fontSize: 12.5, color: K.danger, marginBottom: 10 }}>{t("loadJobError", error instanceof Error ? error.message : String(error))}</div>}
      {!job ? <div style={{ fontSize: 12.5, color: K.muted }}>{t("loadingShort")}</div> : (
        <>
          {job.error && job.status !== "succeeded" && (
            <div style={{ fontSize: 12.5, color: K.danger, background: K.dangerSoft, border: `1px solid ${K.dangerBorder}`, borderRadius: 8, padding: "8px 10px", marginBottom: 12, whiteSpace: "pre-wrap" }}>{job.error}</div>
          )}
          {isJevHeld(job) && (
            <div style={{ fontSize: 12.5, color: K.body, marginBottom: 12 }}>{t("jevHeld")}</div>
          )}

          {prog && (
            <div style={{ marginBottom: 14 }}>
              <Eyebrow>{t("progress")}</Eyebrow>
              <div style={{ fontSize: 12.5, color: K.body, marginBottom: 4 }}>
                {prog.step || t("working")} · <span style={{ fontFamily: mono }}>{prog.done}/{prog.total}</span>
                {job.status === "running" && eta && <span style={{ color: K.muted }}>{t("eta", eta)}</span>}
              </div>
              <div style={{ height: 6, background: K.line2, borderRadius: 3, overflow: "hidden" }}>
                <div style={{ width: `${Math.min(100, (prog.done / prog.total) * 100)}%`, height: "100%", background: K.accent }} />
              </div>
            </div>
          )}

          <Eyebrow>{t("lifecycle")}</Eyebrow>
          <Row label={t("jobId")}><span style={{ fontFamily: mono }}>{job.id}</span></Row>
          <Row label={t("attempt")}>{job.attempt}/{job.max_attempts}</Row>
          <Row label={t("releasedOnShutdown")}>
            <span style={{ color: rel >= maxReleases - 1 && rel > 0 ? K.danger : K.body }}>{rel}/{maxReleases}</span>
            {rel > 0 && <span style={{ color: K.muted }}>{t("releasedNote", maxReleases)}</span>}
          </Row>
          <Row label={t("created")}>{fmtTime(job.created_at)}{job.created_by ? t("byWho", job.created_by) : ""}</Row>
          <Row label={t("startedFinished")}>{fmtTime(job.started_at)} → {fmtTime(job.finished_at)}</Row>
          <Row label={t("duration")}>
            {secs == null ? "—" : fmtSeconds(secs)}
            {expectedSeconds && secs != null && secs > expectedSeconds && job.status === "running" && (
              <span style={{ color: K.amber }}>{t("longer", fmtSeconds(expectedSeconds))}</span>
            )}
          </Row>
          {job.status === "running" && <Row label={t("lease")}>{t("leaseUntil", job.locked_by ?? "—", fmtTime(job.locked_until))}</Row>}
          {job.status === "queued" && job.run_after && <Row label={t("runsAfter")}>{fmtTime(job.run_after)}</Row>}

          <div style={{ marginTop: 16 }}>
            <Eyebrow>{t("cost")}</Eyebrow>
            <div style={{ display: "flex", gap: 20, fontSize: 12.5, flexWrap: "wrap" }}>
              <div>{t("total")} <span style={{ fontFamily: mono, color: K.ink }}>{usd(total)}</span></div>
              <div>{t("llm")} <span style={{ fontFamily: mono, color: K.ink }}>{usd(llmCost)}</span> <span style={{ color: K.muted }}>{t("calls", llm?.totals.calls ?? 0)}</span></div>
              <div>{t("other")} <span style={{ fontFamily: mono, color: K.ink }}>{usd(Math.max(0, total - llmCost))}</span></div>
              {/* AA-804: the job's calls across every paid ledger, on the AI providers page */}
              <a href={traceHref(job.id)} data-testid="job-drawer-open-trace" title={t("openTraceTitle")}
                style={{ color: K.accent }}>{t("openTrace")}</a>
            </div>
          </div>

          {jev && <JevSection jev={jev} />}

          {llm && llm.totals.calls > 0 && (
            <div style={{ marginTop: 16 }}>
              <Eyebrow>{t("llmByStage")}</Eyebrow>
              <DataTable<StageRow>
                data={llm.by_stage} columns={stageColumns} tableId="job-drawer-llm-stages"
                getRowId={s => `${s.stage}:${s.model}`} searchable={false} paginate={false} stickyHeader={false}
              />

              <div style={{ marginTop: 12 }}>
                <Eyebrow>{t("recentCalls")}{llm.truncated ? t("latestOf", llm.calls.length, llm.totals.calls) : ""}</Eyebrow>
                <DataTable<CallRow>
                  data={callRows} columns={callColumns} tableId="job-drawer-llm-calls"
                  getRowId={c => String(c._i)} searchable={false} paginate={false} maxBodyHeight={260}
                />
              </div>
            </div>
          )}

          {links && (links.tour_versions.length > 0 || links.content_pieces.length > 0 || links.tour_id) && (
            <div style={{ marginTop: 16 }}>
              <Eyebrow>{t("wrote")}</Eyebrow>
              {links.tour_id && (
                <Row label={t("tour")}>
                  <span style={{ fontFamily: mono }}>{links.tour_id}</span>
                  {" · "}<a href={`/admin/jobs?tour_id=${links.tour_id}`} style={{ color: K.accent }}>{t("allJobsTour")}</a>
                  {links.version_id && <span style={{ color: K.muted }}> · {t("version")} <span style={{ fontFamily: mono }}>{links.version_id.slice(0, 8)}</span></span>}
                </Row>
              )}
              {links.tour_versions.map(v => (
                <Row key={v.version_id} label={t("tenantVersion")}>
                  <span style={{ fontFamily: mono }}>v{v.version_number} · {v.version_id.slice(0, 8)}</span> · {v.status}
                  <span style={{ color: K.muted }}> · {t("tenant")} <a href={tenantHref(v.tenant_id)} style={{ color: K.accent }}><TenantName id={v.tenant_id} /></a></span>
                </Row>
              ))}
              {links.content_pieces.map(p => (
                <Row key={p.piece_id} label={t("contentPiece")}>
                  <span style={{ fontFamily: mono }}>{p.piece_id.slice(0, 8)}</span> · {p.status}
                  <span style={{ color: K.muted }}> · {t("tenant")} <a href={tenantHref(p.tenant_id)} style={{ color: K.accent }}><TenantName id={p.tenant_id} /></a></span>
                </Row>
              ))}
            </div>
          )}

          <Json label={t("payload")} value={withAccountNames(job.payload, awsAccounts, accountText)} />
          <Json label={t("progress")} value={job.progress} />
          <Json label={t("result")} value={withAccountNames(job.result, awsAccounts, accountText)} />
        </>
      )}
    </Drawer>
  );
}
