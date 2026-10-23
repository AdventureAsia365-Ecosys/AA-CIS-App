"use client";
// app/admin/data-erasure/DataErasure.tsx — AA-839: Platform › Jobs › Data erasure.
//
// The operator side of the data_erasure job kind (runbook AA-Ecosys docs/legal/dsar-erasure-runbook.md, sections 2-5):
//   1. Settings: apply on/off here, tenant retention window, DRY RUN max age, registered tables (GET /erasure).
//   2. Subject + DRY RUN: an offboarded tenant, or a traveller by the sha256 of the address (hashed in the browser).
//      The page polls the job and shows the plan: rows per table and action, blockers, unregistered tables, S3 and
//      secrets, S3 keys held in rows, the per-app checklist, the fingerprint and the tenant state.
//   3. Apply: only for a succeeded, unblocked DRY RUN with apply on (ApplyPanel.tsx). Then the apply result
//      (verification from a new connection, audit row) and a verification DRY RUN.
//   4. History: the recent data_erasure jobs, so the drill is observable.
//
// API through the BFF (/api/admin/job-runner/*; admin only: lib/rbac.ts PAGE_RULES, API_READ_RULES and
// API_WRITE_RULES, backend tenant.read / tenant.write):
//   GET  /api/admin/job-runner/erasure
//   POST /api/admin/job-runner/erasure/run          DRY RUN (default) or apply
//   GET  /api/admin/job-runner/jobs/{id}            polled every 3 s while queued / running only
//   GET  /api/admin/job-runner/jobs?kind=data_erasure  history, polled every 10 s only while a row is active
//   GET  /api/admin/tenants?include_inactive=true   the offboarded tenants for the picker
// The selected DRY RUN and apply job ids live in the URL (?job=…&apply=…), so a reload or a deploy between the
// DRY RUN and the apply keeps the page. The traveller hash is kept only in memory (for the verification run).

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, RefreshCw, RotateCcw } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import {
  Badge, Button, DataTable, ErrorState, K, PageHeader, StatusBadge, TableSkeleton, apiGet, formatDateTime, mono,
  type ColumnDef,
} from "../../_kit";
import {
  ERASURE_API, ERASURE_KIND, JOBS_API, detailMessage, isTerminal, isVerifyRun, jobHash, jobMode, jobSubject,
  jobTenant, totalsSentence,
  type ErasureJob, type ErasureSettings, type JobsPage, type TenantOption,
} from "@/lib/dataErasure";
import ApplyPanel, { type SendResult } from "./ApplyPanel";
import ErasurePlan from "./ErasurePlan";
import SubjectForm from "./SubjectForm";
import { Facts, Notice, Section } from "./erasureUi";

const JOB_POLL_MS = 3_000;
const HISTORY_POLL_MS = 10_000;
const HISTORY_SIZE = 20;

interface View { job: string | null; apply: string | null }

function readView(): View {
  if (typeof window === "undefined") return { job: null, apply: null };
  const p = new URLSearchParams(window.location.search);
  const id = (k: string) => {
    const v = p.get(k);
    return v && /^[0-9a-f-]{36}$/i.test(v) ? v.toLowerCase() : null;
  };
  return { job: id("job"), apply: id("apply") };
}

function writeView(v: View) {
  if (typeof window === "undefined") return;
  const p = new URLSearchParams();
  if (v.job) p.set("job", v.job);
  if (v.apply) p.set("apply", v.apply);
  const qs = p.toString();
  window.history.replaceState(null, "", qs ? `?${qs}` : window.location.pathname);
}

/** POST /erasure/run through the BFF. Never throws; a refusal carries the API's own reason. */
async function runErasure(body: Record<string, unknown>): Promise<SendResult> {
  let res: Response;
  try {
    res = await fetch(`${ERASURE_API}/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch {
    return { ok: false, status: 0, message: "Network error: nothing was sent. Check your connection and try again." };
  }
  let data: Record<string, unknown> = {};
  try {
    data = (await res.json()) as Record<string, unknown>;
  } catch {
    /* non-JSON body */
  }
  if (res.ok) return { ok: true, data };
  return { ok: false, status: res.status, message: detailMessage(res.status, data.detail) };
}

function useJob(id: string | null) {
  return useQuery({
    queryKey: ["erasure-job", id],
    queryFn: () => apiGet<ErasureJob>(`${JOBS_API}/${encodeURIComponent(id!)}`),
    enabled: !!id,
    refetchInterval: (q) => (q.state.data && isTerminal(q.state.data.status) ? false : JOB_POLL_MS),
    refetchIntervalInBackground: false,
  });
}

export default function DataErasure() {
  const qc = useQueryClient();
  const [view, setView] = useState<View>(() => readView());
  const update = (patch: Partial<View>) => setView((prev) => {
    const next = { ...prev, ...patch };
    writeView(next);
    return next;
  });
  // A coarse clock for the DRY RUN age and the retention window (event handler state, not render-time Date.now()).
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 60_000);
    return () => window.clearInterval(id);
  }, []);
  // Traveller hash of this page session only (memory), for the verification DRY RUN after an apply.
  const [travellerHash, setTravellerHash] = useState<string | null>(null);
  const [verifyError, setVerifyError] = useState<string | null>(null);

  const settings = useQuery({
    queryKey: ["erasure-settings"],
    queryFn: () => apiGet<ErasureSettings>(ERASURE_API),
    staleTime: 60_000,
  });
  const tenants = useQuery({
    queryKey: ["admin-tenants", "include-inactive"],
    queryFn: () => apiGet<{ tenants: TenantOption[] }>("/api/admin/tenants?include_inactive=true"),
    staleTime: 60_000,
  });
  const history = useQuery({
    queryKey: ["erasure-history"],
    queryFn: () => apiGet<JobsPage>(`${JOBS_API}?kind=${ERASURE_KIND}&page_size=${HISTORY_SIZE}`),
    refetchInterval: (q) => ((q.state.data?.jobs ?? []).some((j) => !isTerminal(j.status)) ? HISTORY_POLL_MS : false),
    refetchIntervalInBackground: false,
  });
  const dry = useJob(view.job);
  const applied = useJob(view.apply);

  const tenantName = useMemo(() => {
    const m = new Map((tenants.data?.tenants ?? []).map((t) => [t.tenant_id, `${t.name} (${t.slug})`]));
    return (id: string | null) => (id ? m.get(id) ?? id : null);
  }, [tenants.data]);

  const refreshHistory = () => qc.invalidateQueries({ queryKey: ["erasure-history"] });

  const startDryRun = (jobId: string, hash: string | null) => {
    if (hash) setTravellerHash(hash);
    update({ job: jobId, apply: null });
    void refreshHistory();
  };

  const startApply = (jobId: string) => {
    update({ apply: jobId });
    void refreshHistory();
  };

  const applyJob = applied.data;
  const dryJob = dry.data;

  const runVerification = async () => {
    const src = applyJob ?? dryJob;
    if (!src) return;
    setVerifyError(null);
    const subject = jobSubject(src);
    let body: Record<string, unknown>;
    if (subject === "tenant") {
      body = { subject: "tenant", tenant_id: jobTenant(src) };
    } else {
      const hash = travellerHash ?? (dryJob ? jobHash(dryJob) : null);
      if (!hash) {
        setVerifyError("The hash left CIS with the apply. Enter the address again in the form above and run a DRY RUN: every count must be 0.");
        return;
      }
      body = { subject: "traveller", email_sha256: hash, verify: true };
    }
    const out = await runErasure(body);
    if (!out.ok) {
      setVerifyError(out.message);
      return;
    }
    update({ job: String(out.data.job_id), apply: null });
    void refreshHistory();
  };

  const openFromHistory = (j: ErasureJob) => {
    if (jobMode(j) === "apply") {
      const src = j.payload?.dry_run_job_id;
      update({ apply: j.id, job: typeof src === "string" ? src : null });
    } else {
      update({ job: j.id, apply: null });
    }
    window.scrollTo({ top: 0, behavior: "smooth" });
  };

  const s = settings.data;
  const columns = useMemo<ColumnDef<ErasureJob, unknown>[]>(() => [
    {
      accessorKey: "created_at",
      header: "Started",
      cell: (c) => <span style={{ fontSize: 12.5, whiteSpace: "nowrap" }}>{formatDateTime(c.row.original.created_at)}</span>,
    },
    {
      id: "subject",
      accessorFn: (j) => jobSubject(j) ?? "",
      header: "Subject",
      cell: (c) => {
        const j = c.row.original;
        return jobSubject(j) === "tenant"
          ? <span style={{ fontSize: 12.5, overflowWrap: "anywhere" }}>{tenantName(jobTenant(j)) ?? "tenant"}</span>
          : <span style={{ fontSize: 12.5 }}>Traveller</span>;
      },
    },
    {
      id: "mode",
      accessorFn: (j) => (jobMode(j) === "apply" ? "apply" : isVerifyRun(j) ? "verify" : "dry_run"),
      header: "Mode",
      cell: (c) => {
        const j = c.row.original;
        return jobMode(j) === "apply" ? <Badge tone="danger" dot={false}>Apply</Badge>
          : isVerifyRun(j) ? <Badge tone="info" dot={false}>Verify</Badge>
            : <Badge tone="neutral" dot={false}>DRY RUN</Badge>;
      },
    },
    { accessorKey: "status", header: "Status", cell: (c) => <StatusBadge status={c.row.original.status} /> },
    {
      id: "totals",
      accessorFn: (j) => totalsSentence(j.result?.totals),
      header: "Rows",
      cell: (c) => {
        const r = c.row.original.result;
        return (
          <span style={{ fontSize: 12.5, color: K.body }}>
            {r ? totalsSentence(r.totals) : "—"}
            {r?.blocked ? <> · <Badge tone="danger" dot={false}>blocked</Badge></> : null}
            {jobMode(c.row.original) === "apply" && r?.verified !== undefined
              ? <> · <Badge tone={r.verified ? "success" : "danger"} dot={false}>{r.verified ? "verified" : "not verified"}</Badge></>
              : null}
          </span>
        );
      },
    },
    {
      id: "fingerprint",
      accessorFn: (j) => j.result?.fingerprint ?? "",
      header: "Fingerprint",
      cell: (c) => <span style={{ fontFamily: mono, fontSize: 12 }}>{c.row.original.result?.fingerprint?.slice(0, 8) ?? "—"}</span>,
    },
    {
      accessorKey: "created_by",
      header: "By",
      cell: (c) => <span style={{ fontSize: 12, color: K.muted, overflowWrap: "anywhere" }}>{c.row.original.created_by ?? "—"}</span>,
    },
    {
      id: "open",
      header: "",
      enableSorting: false,
      cell: (c) => (
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }} onClick={(e) => e.stopPropagation()}>
          <Button size="sm" variant="secondary" onClick={() => openFromHistory(c.row.original)}>Open</Button>
          <a
            href={`/admin/jobs?job=${encodeURIComponent(c.row.original.id)}`}
            style={{ display: "inline-flex", alignItems: "center", gap: 4, fontSize: 12, color: K.accentDeep }}
          >
            Job <ExternalLink size={11} />
          </a>
        </div>
      ),
    },
    // openFromHistory only calls update (stable setState); tenant names change with the tenants query
    // eslint-disable-next-line react-hooks/exhaustive-deps
  ], [tenantName]);

  if (settings.isLoading) {
    return (
      <div>
        <PageHeader title="Data erasure" description="Traveller DSARs and offboarded tenants: DRY RUN, approve, apply, verify." />
        <TableSkeleton rows={4} cols={4} />
      </div>
    );
  }
  if (settings.error) {
    return (
      <div>
        <PageHeader title="Data erasure" />
        <ErrorState message={(settings.error as Error).message} onRetry={() => void settings.refetch()} />
      </div>
    );
  }

  // Hidden as soon as an apply id is in view (not only once the apply job has loaded): a second click on
  // "Apply erasure" while the new job loads would enqueue a second apply (the job refuses it, single-use DRY RUN).
  const showApplyPanel = !!dryJob && dryJob.status === "succeeded" && jobMode(dryJob) === "dry_run" && !view.apply;

  return (
    <div data-testid="data-erasure-ready" style={{ minWidth: 0 }}>
      <PageHeader
        title="Data erasure"
        description={
          <>
            Traveller DSARs and offboarded tenants in CIS: DRY RUN, approval of the fingerprint, snapshot, apply, verify. Runbook:
            AA-Ecosys <span style={{ fontFamily: mono }}>docs/legal/dsar-erasure-runbook.md</span>.
          </>
        }
        actions={
          <Button variant="ghost" size="sm" onClick={() => { void settings.refetch(); void refreshHistory(); }}>
            <RefreshCw size={13} /> Refresh
          </Button>
        }
      />

      <Section title="Settings on this environment" testId="erasure-settings">
        <Facts items={[
          {
            label: "Apply",
            value: s?.apply_enabled
              ? <Badge tone="danger">Enabled</Badge>
              : <Badge tone="neutral">Off (DRY RUN only)</Badge>,
          },
          { label: "Tenant retention", value: `${s?.tenant_retention_days ?? "—"} day(s) after offboarding` },
          { label: "DRY RUN valid for", value: `${s?.dry_run_max_age_hours ?? "—"} h` },
          { label: "Registered tenant tables", value: s?.tenant_tables.length ?? "—" },
          { label: "Traveller scans", value: s?.traveller_scans.length ?? "—" },
        ]} />
        {s?.apply_enabled && (
          <div style={{ marginTop: 12 }}>
            <Notice tone="warning">
              Apply is ON here. Turn it back off (Terraform) as soon as the approved erasure is done.
            </Notice>
          </div>
        )}
      </Section>

      <SubjectForm
        tenants={tenants.data?.tenants}
        tenantsError={tenants.error ? (tenants.error as Error).message : null}
        retentionDays={s?.tenant_retention_days}
        now={now}
        send={runErasure}
        onStarted={startDryRun}
      />

      {view.job && (
        dry.error ? (
          <Section title="DRY RUN">
            <ErrorState message={(dry.error as Error).message} onRetry={() => void dry.refetch()} />
          </Section>
        ) : dryJob ? (
          <ErasurePlan job={dryJob} tenantName={tenantName(jobTenant(dryJob))} />
        ) : (
          <Section title="DRY RUN"><TableSkeleton rows={3} cols={3} /></Section>
        )
      )}

      {showApplyPanel && dryJob && (
        <ApplyPanel job={dryJob} settings={s} now={now} send={runErasure} onStarted={startApply} />
      )}

      {view.apply && (
        applied.error ? (
          <Section title="Apply">
            <ErrorState message={(applied.error as Error).message} onRetry={() => void applied.refetch()} />
          </Section>
        ) : applyJob ? (
          <>
            <ErasurePlan job={applyJob} tenantName={tenantName(jobTenant(applyJob))} />
            {isTerminal(applyJob.status) && (
              <Section title="Next" testId="erasure-next">
                <p style={{ margin: "0 0 10px", fontSize: 13, color: K.body, lineHeight: 1.5 }}>
                  {applyJob.status === "succeeded"
                    ? "Verify with a new DRY RUN (every delete, cascade, detach and merge row must be 0), then do the steps outside the database from the checklist, delete the snapshot and turn apply off."
                    : "The apply failed and committed nothing. Its DRY RUN is used up: run a new DRY RUN and approve its fingerprint."}
                </p>
                {applyJob.status === "succeeded" && (
                  <Button variant="secondary" onClick={runVerification}>
                    <RotateCcw size={13} /> Run verification DRY RUN
                  </Button>
                )}
                {verifyError && <div style={{ marginTop: 10 }}><Notice tone="warning">{verifyError}</Notice></div>}
              </Section>
            )}
          </>
        ) : (
          <Section title="Apply"><TableSkeleton rows={3} cols={3} /></Section>
        )
      )}

      <Section title="Recent erasure jobs" testId="erasure-history">
        <DataTable
          tableId="data-erasure-history"
          data={history.data?.jobs ?? []}
          columns={columns}
          getRowId={(j) => j.id}
          loading={history.isLoading}
          error={history.error ? (history.error as Error).message : null}
          onRetry={() => void history.refetch()}
          paginate={false}
          tableMinWidth={860}
          emptyTitle="No erasure jobs yet"
          emptyDescription="A DRY RUN started above shows up here."
        />
      </Section>
    </div>
  );
}
