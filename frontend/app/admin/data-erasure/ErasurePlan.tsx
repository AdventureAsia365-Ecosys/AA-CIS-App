"use client";
// app/admin/data-erasure/ErasurePlan.tsx — AA-839: what a data_erasure job found (DRY RUN) or did (apply).
// Reads the job result shape of services/jobs/erasure_job.py run_erasure / _apply.

import { useMemo, useState } from "react";
import {
  Badge, Button, DataTable, K, StatusBadge, formatDateTime, mono, type ColumnDef,
} from "../../_kit";
import {
  jobMode, jobSubject, isVerifyRun,
  type Blocker, type ChecklistItem, type ErasureJob, type PlanRow,
} from "@/lib/dataErasure";
import { ActionBadge, Facts, Fingerprint, Notice, Section, TotalsRow } from "./erasureUi";

function rowColumns(withVia: boolean): ColumnDef<PlanRow, unknown>[] {
  const cols: ColumnDef<PlanRow, unknown>[] = [
    {
      accessorKey: "table",
      header: "Table",
      cell: (c) => <span style={{ fontFamily: mono, fontSize: 12.5, color: K.ink, overflowWrap: "anywhere" }}>{c.row.original.table}</span>,
    },
    { accessorKey: "action", header: "Action", cell: (c) => <ActionBadge action={c.row.original.action} /> },
    {
      accessorKey: "rows",
      header: "Rows",
      cell: (c) => (
        <span style={{ fontFamily: mono, fontSize: 13, color: c.row.original.rows ? K.ink : K.muted }}>
          {c.row.original.rows.toLocaleString("en-US")}
        </span>
      ),
    },
    {
      id: "reason",
      accessorFn: (r) => r.reason ?? "",
      header: "Why",
      cell: (c) => (
        <div style={{ fontSize: 12.5, color: K.body, maxWidth: 420 }}>
          {c.row.original.reason}
          {withVia && c.row.original.via?.length ? (
            <div style={{ color: K.muted, fontFamily: mono, fontSize: 11.5, marginTop: 2, overflowWrap: "anywhere" }}>
              via {c.row.original.via.join(", ")}
            </div>
          ) : null}
        </div>
      ),
    },
  ];
  return cols;
}

function BlockerList({ items, tone }: { items: Blocker[]; tone: "danger" | "info" }) {
  return (
    <ul style={{ margin: 0, paddingLeft: 18, display: "flex", flexDirection: "column", gap: 6, fontSize: 13, color: K.body }}>
      {items.map((b, i) => (
        <li key={`${b.kind}-${b.table}-${i}`} style={{ overflowWrap: "anywhere" }}>
          <Badge tone={tone === "danger" ? "danger" : "neutral"} dot={false}>{b.kind}</Badge>{" "}
          <span style={{ fontFamily: mono, fontSize: 12.5 }}>{b.table}</span>
          {b.rows !== null && <span style={{ fontFamily: mono }}> · {b.rows}</span>} — {b.detail}
        </li>
      ))}
    </ul>
  );
}

function Checklist({ items }: { items: ChecklistItem[] }) {
  const byOwner = useMemo(() => {
    const m = new Map<string, ChecklistItem[]>();
    for (const it of items) m.set(it.owner, [...(m.get(it.owner) ?? []), it]);
    return [...m.entries()];
  }, [items]);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {byOwner.map(([owner, list]) => (
        <div key={owner}>
          <div style={{ fontSize: 12.5, fontWeight: 700, color: K.ink, marginBottom: 4 }}>{owner}</div>
          <ul style={{ margin: 0, paddingLeft: 18, display: "flex", flexDirection: "column", gap: 4 }}>
            {list.map((it, i) => (
              <li key={i} style={{ fontSize: 13, color: K.body, overflowWrap: "anywhere" }}>
                {it.item}
                {it.how && <div style={{ fontSize: 12, color: K.muted }}>{it.how}</div>}
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}

function Sub({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{ marginTop: 16, minWidth: 0 }}>
      <h3 style={{ margin: "0 0 8px", fontSize: 13, fontWeight: 700, color: K.ink }}>{title}</h3>
      {children}
    </div>
  );
}

function OutsideDb({ job }: { job: ErasureJob }) {
  const r = job.result ?? {};
  const s3 = r.s3 ?? [];
  const secrets = r.secrets ?? [];
  const refs = r.s3_refs ?? [];
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10, fontSize: 13, color: K.body }}>
      <div>
        <strong style={{ color: K.ink }}>S3 prefixes</strong>
        {s3.length === 0 ? <div style={{ color: K.muted }}>None listed.</div> : (
          <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
            {s3.map((p) => (
              <li key={p.prefix} style={{ overflowWrap: "anywhere" }}>
                <span style={{ fontFamily: mono, fontSize: 12.5 }}>{p.prefix}</span> —{" "}
                {p.error ? <span style={{ color: K.danger }}>not checked ({p.error}): check by hand</span>
                  : `${p.objects ?? 0} object(s)`}
              </li>
            ))}
          </ul>
        )}
      </div>
      <div>
        <strong style={{ color: K.ink }}>Secrets</strong>
        {secrets.length === 0 ? <div style={{ color: K.muted }}>None listed.</div> : (
          <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
            {secrets.map((s) => (
              <li key={s.name} style={{ overflowWrap: "anywhere" }}>
                <span style={{ fontFamily: mono, fontSize: 12.5 }}>{s.name}</span> —{" "}
                {s.error ? <span style={{ color: K.danger }}>not checked ({s.error}): check by hand</span>
                  : s.exists ? "exists" : "absent"}
              </li>
            ))}
          </ul>
        )}
      </div>
      <div>
        <strong style={{ color: K.ink }}>S3 keys held in rows the apply deletes</strong>
        {refs.length === 0 ? <div style={{ color: K.muted }}>No S3 key columns in this plan.</div> : (
          <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
            {refs.map((ref) => (
              <li key={ref.column} style={{ overflowWrap: "anywhere" }}>
                <span style={{ fontFamily: mono, fontSize: 12.5 }}>{ref.column}</span> —{" "}
                {ref.error ? <span style={{ color: K.danger }}>not readable ({ref.error})</span> : (
                  <>
                    {ref.rows ?? 0} row(s), {ref.outside_prefixes ?? 0} outside the tenant prefixes
                    {ref.outside_keys?.length ? (
                      <div style={{ fontFamily: mono, fontSize: 11.5, color: K.muted }}>
                        Copy into the work item before the apply: {ref.outside_keys.join(", ")}
                      </div>
                    ) : null}
                  </>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

/** The plan (DRY RUN) or the outcome (apply) of one data_erasure job. */
export default function ErasurePlan({ job, tenantName }: { job: ErasureJob; tenantName?: string | null }) {
  const [showOrder, setShowOrder] = useState(false);
  const r = job.result;
  const subject = jobSubject(job);
  const mode = jobMode(job);
  const progress = job.progress as { phase?: string } | null;

  const header = (
    <Facts items={[
      { label: "Job", value: <span style={{ fontFamily: mono, fontSize: 12.5 }}>{job.id}</span> },
      {
        label: "Status",
        value: <StatusBadge status={job.status} label={job.status === "running" && progress?.phase ? `Running · ${progress.phase}` : undefined} />,
      },
      { label: "Mode", value: mode === "apply" ? "Apply" : isVerifyRun(job) ? "DRY RUN (verification)" : "DRY RUN" },
      {
        label: "Subject",
        value: subject === "tenant" ? (tenantName ?? r?.tenant ?? "tenant") : "Traveller (hash only)",
      },
      { label: "Finished", value: job.finished_at ? formatDateTime(job.finished_at) : "—" },
    ]} />
  );

  if (!r || job.status === "queued" || job.status === "running") {
    return (
      <Section title={mode === "apply" ? "Apply" : "DRY RUN"} testId="erasure-plan">
        {header}
        <p style={{ fontSize: 13, color: K.muted, marginBottom: 0 }}>
          {job.status === "queued" ? "Waiting for the worker to pick the job up…"
            : job.status === "running" ? (mode === "apply" ? "Applying…" : "Counting…")
              : job.error ? "The job ended with an error:" : "The job ended without a result."}
        </p>
        {job.error && <Notice tone="danger">{job.error}</Notice>}
      </Section>
    );
  }

  const rows = (mode === "apply" ? r.applied : r.rows) ?? [];
  return (
    <Section
      title={mode === "apply" ? "Apply result" : "DRY RUN plan"}
      testId="erasure-plan"
      right={r.blocked ? <Badge tone="danger">Blocked</Badge> : mode === "apply"
        ? <Badge tone={r.verified ? "success" : "danger"}>{r.verified ? "Verified" : "Not verified"}</Badge>
        : <Badge tone="success">No blockers</Badge>}
    >
      {header}
      {job.error && <div style={{ marginTop: 12 }}><Notice tone="danger">{job.error}</Notice></div>}
      {r.redacted && <div style={{ marginTop: 12 }}><Notice tone="info">Your role sees only the counts of this job.</Notice></div>}

      {subject === "tenant" && mode === "dry_run" && (
        <div style={{ marginTop: 12 }}>
          <Facts items={[
            {
              label: "Tenant state",
              value: <Badge tone={r.tenant_state === "offboarded" ? "warning" : r.tenant_state ? "danger" : "neutral"}>{r.tenant_state ?? "gone"}</Badge>,
            },
            { label: "Offboarded", value: r.offboarded_at ? formatDateTime(r.offboarded_at) : "—" },
            { label: "Checks passed", value: (r.checks_passed ?? []).length },
            { label: "Unregistered tables", value: (r.unregistered ?? []).length },
          ]} />
        </div>
      )}

      <Sub title={mode === "apply" ? "Rows changed" : "Rows per action"}>
        <TotalsRow totals={r.totals} testId="erasure-totals" />
      </Sub>

      {r.fingerprint && (
        <Sub title="Fingerprint (what is approved)">
          <Fingerprint value={r.fingerprint} />
        </Sub>
      )}

      {mode === "apply" && (
        <Sub title="Verification (new connection)">
          {r.verified ? (
            <Notice tone="success">
              A recount from a new database connection found 0 rows left. One audit row was written
              ({subject === "tenant" ? <code>tenant.erased</code> : <code>dsar.cis_erased</code>}, counts only) — see{" "}
              <a href="/admin/platform/audit" style={{ color: K.accentDeep }}>Platform › Audit log</a>.
            </Notice>
          ) : (
            <Notice tone="danger">
              Rows left after the apply: {Object.entries(r.verified_left ?? {}).map(([t, n]) => `${t} ${n}`).join(", ") || "unknown"}.
            </Notice>
          )}
        </Sub>
      )}

      {(r.blockers ?? []).length > 0 && (
        <Sub title={`Blockers (${r.blockers!.length})`}>
          <BlockerList items={r.blockers!} tone="danger" />
        </Sub>
      )}
      {(r.unregistered ?? []).length > 0 && (
        <Sub title="Unregistered tables (block the apply)">
          <div style={{ fontFamily: mono, fontSize: 12.5, color: K.danger, overflowWrap: "anywhere" }}>{r.unregistered!.join(", ")}</div>
        </Sub>
      )}
      {(r.missing_tables ?? []).length > 0 && (
        <Sub title="Inventory tables missing here">
          <div style={{ fontFamily: mono, fontSize: 12.5, color: K.muted, overflowWrap: "anywhere" }}>{r.missing_tables!.join(", ")}</div>
        </Sub>
      )}

      {rows.length > 0 && (
        <Sub title={`Per table (${rows.length})`}>
          <DataTable
            tableId={`erasure-rows-${mode}`}
            data={rows}
            columns={rowColumns(mode === "dry_run")}
            getRowId={(row) => `${row.table}|${row.action}`}
            searchable
            searchPlaceholder="Filter tables…"
            pageSize={25}
            enableCsv={!r.redacted}
            csvFilename={`erasure-${mode}-${job.id.slice(0, 8)}.csv`}
            tableMinWidth={560}
            emptyTitle="No rows"
          />
        </Sub>
      )}

      {subject === "traveller" && (r.manual ?? []).length > 0 && (
        <Sub title="For a person to handle (not changed by the job)">
          <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13, color: K.body }}>
            {r.manual!.map((m) => <li key={m.table}><span style={{ fontFamily: mono }}>{m.table}</span> — {m.rows} row(s). {m.reason}</li>)}
          </ul>
        </Sub>
      )}
      {subject === "traveller" && (r.skipped_tables ?? []).length > 0 && (
        <Sub title="Tables not present here (skipped)">
          <div style={{ fontFamily: mono, fontSize: 12.5, color: K.muted, overflowWrap: "anywhere" }}>{r.skipped_tables!.join(", ")}</div>
        </Sub>
      )}

      {subject === "tenant" && mode === "dry_run" && !r.redacted && (
        <Sub title="Outside the database">
          <OutsideDb job={job} />
        </Sub>
      )}

      {subject === "tenant" && (r.delete_order ?? []).length > 0 && (
        <Sub title="Delete order">
          <Button size="sm" variant="ghost" onClick={() => setShowOrder((v) => !v)}>
            {showOrder ? "Hide" : `Show ${r.delete_order!.length} tables, children first`}
          </Button>
          {showOrder && (
            <ol style={{ margin: "8px 0 0", paddingLeft: 22, fontFamily: mono, fontSize: 12, color: K.body }}>
              {(r.null_first ?? []).map((c) => <li key={`nf-${c}`}>null first: {c}</li>)}
              {r.delete_order!.map((t) => <li key={t}>{t}</li>)}
            </ol>
          )}
        </Sub>
      )}

      {(r.checks_passed ?? []).length > 0 && (
        <Sub title={`Checks passed (${r.checks_passed!.length})`}>
          <BlockerList items={r.checks_passed!} tone="info" />
        </Sub>
      )}

      {(r.checklist ?? []).length > 0 && (
        <Sub title="Per-app checklist (outside CIS, by hand)">
          <Checklist items={r.checklist!} />
        </Sub>
      )}
    </Section>
  );
}
