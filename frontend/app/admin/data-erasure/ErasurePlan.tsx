"use client";
// app/admin/data-erasure/ErasurePlan.tsx — AA-839: what a data_erasure job found (DRY RUN) or did (apply).
// Reads the job result shape of services/jobs/erasure_job.py run_erasure / _apply.

import { useMemo, useState } from "react";
import {
  Badge, Button, DataTable, K, StatusBadge, mono, useFormat, useT, type ColumnDef, type Translator,
} from "../../_kit";
import {
  jobMode, jobSubject, isVerifyRun,
  type Blocker, type ChecklistItem, type ErasureJob, type PlanRow,
} from "@/lib/dataErasure";
import { ActionBadge, Facts, Fingerprint, Notice, Section, TotalsRow } from "./erasureUi";
import { ER } from "./messages";

type T = Translator<typeof ER.en>;

function rowColumns(withVia: boolean, t: T): ColumnDef<PlanRow, unknown>[] {
  const cols: ColumnDef<PlanRow, unknown>[] = [
    {
      accessorKey: "table",
      header: t("colTable"),
      cell: (c) => <span style={{ fontFamily: mono, fontSize: 12.5, color: K.ink, overflowWrap: "anywhere" }}>{c.row.original.table}</span>,
    },
    { accessorKey: "action", header: t("colAction"), cell: (c) => <ActionBadge action={c.row.original.action} /> },
    {
      accessorKey: "rows",
      header: t("colRows"),
      cell: (c) => (
        <span style={{ fontFamily: mono, fontSize: 13, color: c.row.original.rows ? K.ink : K.muted }}>
          {c.row.original.rows.toLocaleString("en-US")}
        </span>
      ),
    },
    {
      id: "reason",
      accessorFn: (r) => r.reason ?? "",
      header: t("colWhy"),
      cell: (c) => (
        <div style={{ fontSize: 12.5, color: K.body, maxWidth: 420 }}>
          {c.row.original.reason}
          {withVia && c.row.original.via?.length ? (
            <div style={{ color: K.muted, fontFamily: mono, fontSize: 11.5, marginTop: 2, overflowWrap: "anywhere" }}>
              {t("via", c.row.original.via.join(", "))}
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
  const t = useT(ER);
  const r = job.result ?? {};
  const s3 = r.s3 ?? [];
  const secrets = r.secrets ?? [];
  const refs = r.s3_refs ?? [];
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10, fontSize: 13, color: K.body }}>
      <div>
        <strong style={{ color: K.ink }}>{t("s3Prefixes")}</strong>
        {s3.length === 0 ? <div style={{ color: K.muted }}>{t("noneListed")}</div> : (
          <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
            {s3.map((p) => (
              <li key={p.prefix} style={{ overflowWrap: "anywhere" }}>
                <span style={{ fontFamily: mono, fontSize: 12.5 }}>{p.prefix}</span> —{" "}
                {p.error ? <span style={{ color: K.danger }}>{t("notChecked", p.error)}</span>
                  : t("objects", p.objects ?? 0)}
              </li>
            ))}
          </ul>
        )}
      </div>
      <div>
        <strong style={{ color: K.ink }}>{t("secrets")}</strong>
        {secrets.length === 0 ? <div style={{ color: K.muted }}>{t("noneListed")}</div> : (
          <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
            {secrets.map((s) => (
              <li key={s.name} style={{ overflowWrap: "anywhere" }}>
                <span style={{ fontFamily: mono, fontSize: 12.5 }}>{s.name}</span> —{" "}
                {s.error ? <span style={{ color: K.danger }}>{t("notChecked", s.error)}</span>
                  : s.exists ? t("exists") : t("absent")}
              </li>
            ))}
          </ul>
        )}
      </div>
      <div>
        <strong style={{ color: K.ink }}>{t("s3Keys")}</strong>
        {refs.length === 0 ? <div style={{ color: K.muted }}>{t("noS3Columns")}</div> : (
          <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
            {refs.map((ref) => (
              <li key={ref.column} style={{ overflowWrap: "anywhere" }}>
                <span style={{ fontFamily: mono, fontSize: 12.5 }}>{ref.column}</span> —{" "}
                {ref.error ? <span style={{ color: K.danger }}>{t("notReadable", ref.error)}</span> : (
                  <>
                    {t("rowsOutside", ref.rows ?? 0, ref.outside_prefixes ?? 0)}
                    {ref.outside_keys?.length ? (
                      <div style={{ fontFamily: mono, fontSize: 11.5, color: K.muted }}>
                        {t("copyBefore", ref.outside_keys.join(", "))}
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
  const t = useT(ER);
  const { formatDateTime } = useFormat();
  const [showOrder, setShowOrder] = useState(false);
  const r = job.result;
  const subject = jobSubject(job);
  const mode = jobMode(job);
  const progress = job.progress as { phase?: string } | null;

  const header = (
    <Facts items={[
      { label: t("factJob"), value: <span style={{ fontFamily: mono, fontSize: 12.5 }}>{job.id}</span> },
      {
        label: t("factStatus"),
        value: <StatusBadge status={job.status} label={job.status === "running" && progress?.phase ? t("runningPhase", progress.phase) : undefined} />,
      },
      { label: t("factMode"), value: mode === "apply" ? t("modeApply") : isVerifyRun(job) ? t("modeDryVerify") : "DRY RUN" },
      {
        label: t("factSubject"),
        value: subject === "tenant" ? (tenantName ?? r?.tenant ?? t("tenantWord")) : t("travellerHashOnly"),
      },
      { label: t("factFinished"), value: job.finished_at ? formatDateTime(job.finished_at) : "—" },
    ]} />
  );

  if (!r || job.status === "queued" || job.status === "running") {
    return (
      <Section title={mode === "apply" ? t("modeApply") : "DRY RUN"} testId="erasure-plan">
        {header}
        <p style={{ fontSize: 13, color: K.muted, marginBottom: 0 }}>
          {job.status === "queued" ? t("waitingWorker")
            : job.status === "running" ? (mode === "apply" ? t("applying") : t("counting"))
              : job.error ? t("endedError") : t("endedNoResult")}
        </p>
        {job.error && <Notice tone="danger">{job.error}</Notice>}
      </Section>
    );
  }

  const rows = (mode === "apply" ? r.applied : r.rows) ?? [];
  return (
    <Section
      title={mode === "apply" ? t("applyResult") : t("dryPlan")}
      testId="erasure-plan"
      right={r.blocked ? <Badge tone="danger">{t("blocked")}</Badge> : mode === "apply"
        ? <Badge tone={r.verified ? "success" : "danger"}>{r.verified ? t("verified") : t("notVerified")}</Badge>
        : <Badge tone="success">{t("noBlockers")}</Badge>}
    >
      {header}
      {job.error && <div style={{ marginTop: 12 }}><Notice tone="danger">{job.error}</Notice></div>}
      {r.redacted && <div style={{ marginTop: 12 }}><Notice tone="info">{t("redacted")}</Notice></div>}

      {subject === "tenant" && mode === "dry_run" && (
        <div style={{ marginTop: 12 }}>
          <Facts items={[
            {
              label: t("factTenantState"),
              value: <Badge tone={r.tenant_state === "offboarded" ? "warning" : r.tenant_state ? "danger" : "neutral"}>{r.tenant_state ?? t("stateGone")}</Badge>,
            },
            { label: t("factOffboarded"), value: r.offboarded_at ? formatDateTime(r.offboarded_at) : "—" },
            { label: t("factChecksPassed"), value: (r.checks_passed ?? []).length },
            { label: t("factUnregistered"), value: (r.unregistered ?? []).length },
          ]} />
        </div>
      )}

      <Sub title={mode === "apply" ? t("rowsChanged") : t("rowsPerAction")}>
        <TotalsRow totals={r.totals} testId="erasure-totals" />
      </Sub>

      {r.fingerprint && (
        <Sub title={t("fingerprintApproved")}>
          <Fingerprint value={r.fingerprint} />
        </Sub>
      )}

      {mode === "apply" && (
        <Sub title={t("verification")}>
          {r.verified ? (
            <Notice tone="success">
              {t("verifiedLead")}
              {subject === "tenant" ? <code>tenant.erased</code> : <code>dsar.cis_erased</code>}{t("verifiedMid")}{" "}
              <a href="/admin/platform/audit" style={{ color: K.accentDeep }}>{t("auditLink")}</a>.
            </Notice>
          ) : (
            <Notice tone="danger">
              {t("rowsLeft", Object.entries(r.verified_left ?? {}).map(([tb, n]) => `${tb} ${n}`).join(", ") || t("unknown"))}
            </Notice>
          )}
        </Sub>
      )}

      {(r.blockers ?? []).length > 0 && (
        <Sub title={t("blockers", r.blockers!.length)}>
          <BlockerList items={r.blockers!} tone="danger" />
        </Sub>
      )}
      {(r.unregistered ?? []).length > 0 && (
        <Sub title={t("unregisteredBlock")}>
          <div style={{ fontFamily: mono, fontSize: 12.5, color: K.danger, overflowWrap: "anywhere" }}>{r.unregistered!.join(", ")}</div>
        </Sub>
      )}
      {(r.missing_tables ?? []).length > 0 && (
        <Sub title={t("inventoryMissing")}>
          <div style={{ fontFamily: mono, fontSize: 12.5, color: K.muted, overflowWrap: "anywhere" }}>{r.missing_tables!.join(", ")}</div>
        </Sub>
      )}

      {rows.length > 0 && (
        <Sub title={t("perTable", rows.length)}>
          <DataTable
            tableId={`erasure-rows-${mode}`}
            data={rows}
            columns={rowColumns(mode === "dry_run", t)}
            getRowId={(row) => `${row.table}|${row.action}`}
            searchable
            searchPlaceholder={t("filterTables")}
            pageSize={25}
            enableCsv={!r.redacted}
            csvFilename={`erasure-${mode}-${job.id.slice(0, 8)}.csv`}
            tableMinWidth={560}
            emptyTitle={t("noRows")}
          />
        </Sub>
      )}

      {subject === "traveller" && (r.manual ?? []).length > 0 && (
        <Sub title={t("manualTitle")}>
          <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13, color: K.body }}>
            {r.manual!.map((m) => <li key={m.table}><span style={{ fontFamily: mono }}>{m.table}</span> — {t("manualRows", m.rows)} {m.reason}</li>)}
          </ul>
        </Sub>
      )}
      {subject === "traveller" && (r.skipped_tables ?? []).length > 0 && (
        <Sub title={t("skippedTitle")}>
          <div style={{ fontFamily: mono, fontSize: 12.5, color: K.muted, overflowWrap: "anywhere" }}>{r.skipped_tables!.join(", ")}</div>
        </Sub>
      )}

      {subject === "tenant" && mode === "dry_run" && !r.redacted && (
        <Sub title={t("outsideDb")}>
          <OutsideDb job={job} />
        </Sub>
      )}

      {subject === "tenant" && (r.delete_order ?? []).length > 0 && (
        <Sub title={t("deleteOrder")}>
          <Button size="sm" variant="ghost" onClick={() => setShowOrder((v) => !v)}>
            {showOrder ? t("hide") : t("showOrder", r.delete_order!.length)}
          </Button>
          {showOrder && (
            <ol style={{ margin: "8px 0 0", paddingLeft: 22, fontFamily: mono, fontSize: 12, color: K.body }}>
              {(r.null_first ?? []).map((c) => <li key={`nf-${c}`}>{t("nullFirst", c)}</li>)}
              {r.delete_order!.map((t) => <li key={t}>{t}</li>)}
            </ol>
          )}
        </Sub>
      )}

      {(r.checks_passed ?? []).length > 0 && (
        <Sub title={t("checksPassed", r.checks_passed!.length)}>
          <BlockerList items={r.checks_passed!} tone="info" />
        </Sub>
      )}

      {(r.checklist ?? []).length > 0 && (
        <Sub title={t("checklist")}>
          <Checklist items={r.checklist!} />
        </Sub>
      )}
    </Section>
  );
}
