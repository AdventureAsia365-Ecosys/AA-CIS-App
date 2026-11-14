"use client";
// app/admin/tenants/data-erasure/TenantOffboarding.tsx — the Tenant offboarding tab of Platform › Jobs › Data erasure:
// the offboarding erasure saga across CIS, Booking, TripPlanner and the CRM (backend api/routers/admin_tenant_erasure.py,
// services/tenant_erasure/saga.py).
//
//   1. Settings: the apply switch, the exit windows, how long an approval is valid.
//   2. Requests: one per offboarded tenant (phase, round, the date the DRY RUN round may start, blockers).
//   3. A request: the per-app dates, the steps of the current round (DRY RUN plan hash, apply, census), the blockers,
//      Run now (queue the saga job), Start a new round, Stop (the request stays failed), and the approval form (fingerprint prefix + RDS snapshot id;
//      the API accepts only a Cognito administrator).
//   4. Drill: window 0 for an offboarded synthetic tenant (nothing is approved or applied there).
// API through the BFF (/api/admin/job-runner/erasure/requests*, admin only: lib/rbac.ts `job-runner/erasure`).

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Ban, Play, RefreshCw, RotateCcw, ShieldCheck } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Badge, Button, DataTable, DateText, ErrorState, Input, K, StatusBadge, TableSkeleton, apiGet, mono, useT,
  type ColumnDef,
} from "../../../_kit";
import {
  APPS, ERASURE_REQUESTS_API, FINGERPRINT_PREFIX, WORKING_PHASES, approvalBlockers, decisionActions, notBefore,
  phaseTone, prefixMatches, shortHash, stepOf, stepStatus,
  type App, type SagaDetail, type SagaList, type SagaRequest, type StepMode,
} from "@/lib/tenantErasure";
import { TenantName, TenantSelect, useTenants } from "../../_components/TenantSelect";
import { tenantSearchText } from "@/lib/tenants";
import { Facts, Notice, Section, hintStyle, inputStyle, labelStyle } from "./erasureUi";
import { TO } from "./offboarding.messages";

const LIST_POLL_MS = 15_000;
const APP_LABEL: Record<App, string> = { cis: "CIS", booking: "Booking", tripplanner: "TripPlanner", crm: "CRM" };

function stepTone(s: string): "neutral" | "info" | "success" | "danger" {
  return s === "done" ? "success" : s === "failed" ? "danger" : s === "active" ? "info" : "neutral";
}

type Sent = { ok: true; data: Record<string, unknown> } | { ok: false; status: number; message: string };

async function post(path: string, body: Record<string, unknown>, noAnswer: string): Promise<Sent> {
  let res: Response;
  try {
    res = await fetch(`${ERASURE_REQUESTS_API}${path}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
  } catch {
    return { ok: false, status: 0, message: noAnswer };
  }
  let data: Record<string, unknown> = {};
  try {
    data = (await res.json()) as Record<string, unknown>;
  } catch {
    /* non-JSON body */
  }
  if (res.ok) return { ok: true, data };
  const detail = typeof data.detail === "string" ? data.detail : `HTTP ${res.status}`;
  return { ok: false, status: res.status, message: detail };
}

function StepCell({ req, app, mode }: { req: SagaRequest; app: App; mode: StepMode }) {
  const t = useT(TO);
  const s = stepOf(req, app, mode);
  if (!s) return <span style={{ color: K.muted }}>—</span>;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 3, fontSize: 12 }}>
      <StatusBadge status={s.status === "ok" ? "succeeded" : s.status} label={s.status} />
      {s.plan_hash && <span style={{ fontFamily: mono }}>{t("planHash")} {shortHash(s.plan_hash)}</span>}
      {s.approved_plan_hash && <span style={{ fontFamily: mono }}>{t("approvedHash")} {shortHash(s.approved_plan_hash)}</span>}
      {s.census && <span>{t("piiLeft", s.census.pii_rows_found)}</span>}
      {s.error_code && <span style={{ color: K.muted, fontFamily: mono }}>{s.error_code}</span>}
    </div>
  );
}

type Note = { tone: "success" | "danger"; text: string } | null;
type Act = (path: string, body: Record<string, unknown>, done: string) => Promise<void>;

function DetailActions({ req, busy, act }: { req: SagaRequest; busy: boolean; act: Act }) {
  const t = useT(TO);
  const can = decisionActions(req);
  return (
    <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
      {WORKING_PHASES.includes(req.phase) && (
        <Button size="sm" variant="secondary" disabled={busy}
          onClick={() => void act("/advance", {}, t("runQueued"))}>
          <Play size={13} /> {t("runNow")}
        </Button>
      )}
      {can.newRound && (
        <Button size="sm" variant="ghost" disabled={busy}
          onClick={() => void act("/new-round", {}, t("newRoundDone"))}>
          <RotateCcw size={13} /> {t("newRound")}
        </Button>
      )}
      {can.stop && (
        <Button size="sm" variant="danger" disabled={busy} testId="tenant-erasure-stop"
          onClick={() => { if (window.confirm(t("stopConfirm"))) void act("/stop", {}, t("stopDone")); }}>
          <Ban size={13} /> {t("stop")}
        </Button>
      )}
    </div>
  );
}

function DetailFacts({ req }: { req: SagaRequest }) {
  const t = useT(TO);
  const approval = req.approved_by ? [
    { label: t("factApproved"), value: <span style={{ fontSize: 13 }}>{req.approved_by}</span> },
    { label: t("factExpires"), value: <DateText value={req.approval_expires_at} withTime /> },
    { label: t("factSnapshot"), value: <span style={{ fontFamily: mono, fontSize: 13 }}>{req.snapshot_id}</span> },
  ] : [];
  const job = req.job ? [{ label: t("factJob"), value: <StatusBadge status={req.job.status} /> }] : [];
  return (
    <Facts items={[
      { label: t("colPhase"), value: <Badge tone={phaseTone(req.phase)}>{req.phase}</Badge> },
      { label: t("colMode"), value: req.mode === "drill" ? t("drill") : t("live") },
      { label: t("colRound"), value: req.round },
      { label: t("factOffboarded"), value: <DateText value={req.opened_at} withTime /> },
      ...APPS.map((app) => ({
        label: t("factNotBefore", APP_LABEL[app]), value: <DateText value={notBefore(req, app)} withTime />,
      })),
      { label: t("factFingerprint"), value: <span style={{ fontFamily: mono }}>{shortHash(req.combined_fingerprint)}</span> },
      ...approval,
      ...job,
    ]} />
  );
}

function BlockerList({ req }: { req: SagaRequest }) {
  const t = useT(TO);
  if (req.blockers.length === 0) return null;
  return (
    <div style={{ marginTop: 12 }} data-testid="tenant-erasure-blockers">
      <Notice tone="warning">
        <strong>{t("blockersTitle")}</strong>
        <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
          {req.blockers.map((b) => (
            <li key={`${b.app}:${b.kind}`}>
              <strong>{APP_LABEL[b.app as App] ?? b.app}</strong> · {b.kind}{b.detail ? ` — ${b.detail}` : ""}
            </li>
          ))}
        </ul>
      </Notice>
    </div>
  );
}

function ApprovalForm({ req, closed, busy, act }: { req: SagaRequest; closed: string[]; busy: boolean; act: Act }) {
  const t = useT(TO);
  const [prefix, setPrefix] = useState("");
  const [snapshot, setSnapshot] = useState("");
  const canApprove = closed.length === 0 && prefixMatches(req.combined_fingerprint, prefix) && snapshot.trim() !== "";
  if (closed.length > 0) {
    return (
      <Notice tone="warning">
        <strong>{t("approveClosed")}</strong>
        <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>{closed.map((r) => <li key={r}>{r}</li>)}</ul>
      </Notice>
    );
  }
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <Notice tone="danger">{t("approveLead")}</Notice>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 12 }}>
        <label style={labelStyle}>
          {t("fingerprintPrefix", FINGERPRINT_PREFIX)}
          <Input value={prefix} onChange={(e) => setPrefix(e.target.value)} maxLength={FINGERPRINT_PREFIX}
            autoComplete="off" spellCheck={false} style={{ ...inputStyle, fontFamily: mono }}
            data-testid="tenant-erasure-fp" />
        </label>
        <label style={labelStyle}>
          {t("snapshotId")}
          <Input value={snapshot} onChange={(e) => setSnapshot(e.target.value)} maxLength={255}
            autoComplete="off" spellCheck={false} style={{ ...inputStyle, fontFamily: mono }}
            data-testid="tenant-erasure-snapshot" />
        </label>
      </div>
      <span style={hintStyle}>{t("cognitoNote")}</span>
      <div>
        <Button variant="danger" disabled={!canApprove || busy}
          onClick={() => void act("/approve", { fingerprint: req.combined_fingerprint, snapshot_id: snapshot.trim() },
            t("approved"))}>
          {t("approve")}
        </Button>
      </div>
    </div>
  );
}

function useStepColumns(req: SagaRequest | undefined): ColumnDef<{ app: App }, unknown>[] {
  const t = useT(TO);
  return useMemo(() => {
    if (!req) return [];
    return [
      {
        accessorKey: "app", header: t("colApp"),
        cell: (c) => (
          <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
            <strong>{APP_LABEL[c.row.original.app]}</strong>
            <Badge tone={stepTone(stepStatus(req, c.row.original.app))} dot={false}>{stepStatus(req, c.row.original.app)}</Badge>
          </div>
        ),
      },
      { id: "dry", header: t("colDryRun"), cell: (c) => <StepCell req={req} app={c.row.original.app} mode="dry_run" /> },
      { id: "apply", header: t("colApply"), cell: (c) => <StepCell req={req} app={c.row.original.app} mode="apply" /> },
      { id: "verify", header: t("colVerify"), cell: (c) => <StepCell req={req} app={c.row.original.app} mode="verify" /> },
    ];
  }, [req, t]);
}

function useNow(everyMs: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const h = window.setInterval(() => setNow(Date.now()), everyMs);
    return () => window.clearInterval(h);
  }, [everyMs]);
  return now;
}

function RequestDetail({ id, onChanged }: { id: string; onChanged: () => void }) {
  const t = useT(TO);
  const now = useNow(60_000);
  const q = useQuery({
    queryKey: ["tenant-erasure-request", id],
    queryFn: () => apiGet<SagaDetail>(`${ERASURE_REQUESTS_API}/${encodeURIComponent(id)}`),
    refetchInterval: (s) => (s.state.data && WORKING_PHASES.includes(s.state.data.request.phase) ? LIST_POLL_MS : false),
    refetchIntervalInBackground: false,
  });
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<Note>(null);
  const stepColumns = useStepColumns(q.data?.request);

  if (q.isLoading) return <Section title={t("detailTitle")}><TableSkeleton rows={4} cols={4} /></Section>;
  if (q.error || !q.data) {
    return (
      <Section title={t("detailTitle")}>
        <ErrorState message={(q.error as Error | null)?.message ?? ""} onRetry={() => void q.refetch()} />
      </Section>
    );
  }
  const req = q.data.request;
  const closed = approvalBlockers(req, q.data.settings, now, {
    notAwaiting: t("notAwaiting"), switchOff: t("switchOff"), tooOld: (h: number) => t("tooOld", h),
  });

  const act: Act = async (path, body, done) => {
    setBusy(true);
    setNote(null);
    const out = await post(`/${encodeURIComponent(req.request_id)}${path}`, body, t("noAnswer"));
    setBusy(false);
    setNote(out.ok ? { tone: "success", text: done } : { tone: "danger", text: out.message });
    void q.refetch();
    onChanged();
  };

  return (
    <div data-testid="tenant-erasure-detail">
      <Section title={<>{t("detailTitle")} · <TenantName id={req.tenant_id} /></>}
        right={<DetailActions req={req} busy={busy} act={act} />}>
        {note && <Notice tone={note.tone}>{note.text}</Notice>}
        <DetailFacts req={req} />
        <BlockerList req={req} />
      </Section>

      <Section title={t("stepsTitle", req.round)} testId="tenant-erasure-steps">
        <DataTable tableId="tenant-erasure-steps" data={APPS.map((app) => ({ app }))} columns={stepColumns}
          getRowId={(r) => r.app} paginate={false} tableMinWidth={720} emptyTitle="—" />
      </Section>

      {req.phase === "awaiting_approval" && (
        <Section title={<span style={{ display: "inline-flex", gap: 6, alignItems: "center" }}><ShieldCheck size={15} /> {t("approveTitle")}</span>}
          testId="tenant-erasure-approve">
          <ApprovalForm req={req} closed={closed} busy={busy} act={act} />
        </Section>
      )}

      {req.receipt && (
        <Section title={t("receiptTitle")} testId="tenant-erasure-receipt">
          <pre style={{ margin: 0, fontSize: 12, fontFamily: mono, whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
            {JSON.stringify(req.receipt, null, 2)}
          </pre>
        </Section>
      )}
    </div>
  );
}

function useRequestColumns(onOpen: (requestId: string) => void): ColumnDef<SagaRequest, unknown>[] {
  const t = useT(TO);
  return useMemo<ColumnDef<SagaRequest, unknown>[]>(() => [
    {
      id: "tenant", accessorFn: (r) => r.tenant_name ?? r.tenant_id, header: t("colTenant"),
      cell: (c) => (c.row.original.tenant_name
        ? <TenantName id={c.row.original.tenant_id} style={{ fontSize: 12.5 }} />
        : <span style={{ fontSize: 12.5, fontFamily: mono }}>{c.row.original.tenant_id.slice(0, 8)} · {t("tenantGone")}</span>),
    },
    {
      accessorKey: "mode", header: t("colMode"),
      cell: (c) => <Badge tone={c.row.original.mode === "drill" ? "info" : "neutral"} dot={false}>
        {c.row.original.mode === "drill" ? t("drill") : t("live")}</Badge>,
    },
    { accessorKey: "phase", header: t("colPhase"), cell: (c) => <Badge tone={phaseTone(c.row.original.phase)}>{c.row.original.phase}</Badge> },
    { accessorKey: "round", header: t("colRound") },
    {
      id: "due", accessorFn: (r) => r.next_due_at ?? "", header: t("colDue"),
      cell: (c) => <DateText value={c.row.original.next_due_at} />,
    },
    {
      id: "blockers", accessorFn: (r) => r.blockers.length, header: t("colBlockers"),
      cell: (c) => (c.row.original.blockers.length
        ? <Badge tone="warning" dot={false}>{c.row.original.blockers.length}</Badge> : <span style={{ color: K.muted }}>0</span>),
    },
    { accessorKey: "created_at", header: t("colCreated"), cell: (c) => <DateText value={c.row.original.created_at} withTime /> },
    {
      id: "open", header: "", enableSorting: false,
      cell: (c) => <Button size="sm" variant="secondary" onClick={() => onOpen(c.row.original.request_id)}>{t("open")}</Button>,
    },
  ], [t, onOpen]);
}

function SettingsSection({ s, onRefresh }: { s: SagaList["settings"] | undefined; onRefresh: () => void }) {
  const t = useT(TO);
  const dash = (v: number | undefined) => String(v ?? "—");
  return (
    <Section title={t("settingsTitle")} testId="tenant-erasure-settings"
      right={<Button variant="ghost" size="sm" onClick={onRefresh}><RefreshCw size={13} /> {t("refresh")}</Button>}>
      <Facts items={[
        { label: t("factApply"), value: s?.apply_enabled ? <Badge tone="danger">{t("applyOn")}</Badge> : <Badge tone="neutral">{t("applyOff")}</Badge> },
        { label: t("factWindowOthers"), value: t("days", dash(s?.window_days.cis)) },
        { label: t("factWindowCrm"), value: t("days", dash(s?.window_days.crm)) },
        { label: t("factValid"), value: t("hours", dash(s?.approval_valid_hours)) },
      ]} />
    </Section>
  );
}

function DrillSection({ onStarted }: { onStarted: (requestId: string | undefined) => void }) {
  const t = useT(TO);
  const [drillTenant, setDrillTenant] = useState("");
  const [drillNote, setDrillNote] = useState<Note>(null);
  const startDrill = async () => {
    setDrillNote(null);
    const out = await post("/drill", { tenant_id: drillTenant }, t("noAnswer"));
    if (!out.ok) {
      setDrillNote({ tone: "danger", text: out.message });
      return;
    }
    setDrillNote({ tone: "success", text: t("drillStarted") });
    onStarted(((out.data.request ?? {}) as { request_id?: string }).request_id);
  };
  return (
    <Section title={t("drillTitle")} testId="tenant-erasure-drill">
      <p style={{ ...hintStyle, margin: "0 0 10px" }}>{t("drillHelp")}</p>
      {drillNote && <Notice tone={drillNote.tone}>{drillNote.text}</Notice>}
      <div style={{ display: "flex", gap: 10, alignItems: "flex-end", flexWrap: "wrap" }}>
        <div style={{ minWidth: 260, flex: "1 1 260px" }}>
          <TenantSelect value={drillTenant} onChange={(id) => setDrillTenant(id)} includeInactive
            offer={(tn) => tn.status === "offboarded"} testId="tenant-erasure-drill-tenant" />
        </div>
        <Button variant="secondary" disabled={!drillTenant} onClick={() => void startDrill()}>{t("drillButton")}</Button>
      </div>
    </Section>
  );
}

export default function TenantOffboarding({ active = true }: { active?: boolean }) {
  const t = useT(TO);
  const qc = useQueryClient();
  const [selected, setSelected] = useState<string | null>(null);
  const list = useQuery({
    queryKey: ["tenant-erasure-requests"],
    queryFn: () => apiGet<SagaList>(ERASURE_REQUESTS_API),
    enabled: active,
    refetchInterval: (s) => ((s.state.data?.requests ?? []).some((r) => WORKING_PHASES.includes(r.phase))
      ? LIST_POLL_MS : false),
    refetchIntervalInBackground: false,
  });
  const refresh = useCallback(() => void qc.invalidateQueries({ queryKey: ["tenant-erasure-requests"] }), [qc]);
  const columns = useRequestColumns(setSelected);
  // S280: staff find a request by the tenant's slug or id, or by the request id, not only by the name shown.
  const { byId } = useTenants();
  const requestSearch = useCallback(
    (r: SagaRequest) => tenantSearchText(r.tenant_id, byId, r.tenant_name, r.request_id), [byId]);
  const onDrillStarted = (requestId: string | undefined) => {
    if (requestId) setSelected(requestId);
    refresh();
  };

  if (list.isLoading) return <TableSkeleton rows={4} cols={5} />;
  if (list.error) return <ErrorState message={(list.error as Error).message} onRetry={() => void list.refetch()} />;

  return (
    <div data-testid="tenant-offboarding-ready" style={{ minWidth: 0 }}>
      <p style={{ margin: "0 0 14px", fontSize: 13, color: K.body, lineHeight: 1.55 }}>{t("intro")}</p>
      <SettingsSection s={list.data?.settings} onRefresh={refresh} />

      <Section title={t("requestsTitle")} testId="tenant-erasure-requests">
        <DataTable tableId="tenant-erasure-requests" data={list.data?.requests ?? []} columns={columns}
          getRowId={(r) => r.request_id} paginate={false} tableMinWidth={860} searchText={requestSearch}
          emptyTitle={t("empty")} emptyDescription={t("emptyDesc")} />
      </Section>

      {selected && <RequestDetail key={selected} id={selected} onChanged={refresh} />}

      <DrillSection onStarted={onDrillStarted} />
    </div>
  );
}
