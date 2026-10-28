"use client";
// app/admin/platform/agencies/AgencyDrawer.tsx
// AA-941 (card CRM-13a, screen S-01) — one agency of the partner CRM: usage meters and the L1 support actions.
//
// Reads (GET /api/admin/platform/crm/agencies/{tid}[/usage|entitlements|data-requests|members|jobs]) load when the
// drawer opens and on Refresh; nothing polls. Every write opens WriteModal (confirm + ticket reference) and goes
// through the CIS relay to crm.ops.v1, where the CRM checks the crm-platform-admin group, writes the agency's own
// access log (actor AA ops, ticket id) and refuses what the agency's state does not allow. The CRM's refusal code is
// shown as it is (has_travellers, owner_exists, …).
//
// What AA can and cannot do here (design v3 §4.6, Q1 6.3, Q2 3.12): lock / unlock; invite the FIRST owner of an empty
// agency only; allow features (the owner switches them on); record the insights opt-out the owner asked for; raise an
// export or deletion request the owner must confirm in the CRM (AA never confirms or runs it); start an owner recovery
// (members can cancel it for 14 days, then AA completes it); disable a member; retry a failed job. Never trips,
// pipeline, revenue or traveller data.

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Lock, RefreshCw, Unlock } from "lucide-react";
import { useMemo, useState } from "react";
import {
  ApiError,
  Badge,
  Button,
  DataTable,
  Drawer,
  K,
  RADIUS,
  SkeletonBar,
  SkeletonStyle,
  apiGet,
  formatDateTime,
  mono,
  sans,
  useToast,
  type ColumnDef,
} from "../../../_kit";
import {
  CRM_API,
  DATA_REQUEST_KINDS,
  FEATURES,
  LOCK_REASONS,
  RECOVERY_REASONS,
  crmErrorText,
  crmWrite,
  dataRequestStatus,
  formatBytes,
  formatUsd,
  kindLabel,
  lockSourceText,
  membershipFeed,
  pct,
  recoveryDue,
  shortId,
  stateLabel,
  usdPct,
  type AgencyEntitlements,
  type AgencyMember,
  type AgencyMembers,
  type AgencyUsage,
  type DataRequest,
  type DataRequestKind,
  type DataRequestList,
  type DataRequestRaised,
  type FailedJob,
  type FailedJobList,
  type InsightsOptOut,
  type InsightsOptOutBody,
  type JobRetried,
  type MemberDeactivated,
  type OwnerInviteAccepted,
  type RecoveryCompleted,
  type RecoveryOpened,
  type TenantSummary,
  type TicketBody,
  type WriteResult,
} from "@/lib/crmConsole";
import { Field, InlineError, Section, WriteModal, fieldStyle, noteStyle } from "./consoleUi";

type Props = {
  agency: TenantSummary;
  canWrite: boolean;
  onClose: () => void;
  /** After a write: the new summary when the CRM returned one (lock / unlock), else undefined. */
  onChanged: (next?: TenantSummary) => void;
};

const key = (tid: string, part: string) => ["crm-agency", tid, part] as const;

function errText(e: unknown): string {
  if (e instanceof ApiError) return crmErrorText(e.status, e.message);
  return (e as Error)?.message || "Could not load this part.";
}

export default function AgencyDrawer({ agency, canWrite, onClose, onChanged }: Props) {
  const qc = useQueryClient();
  const toast = useToast();
  const tid = agency.tenant_id;
  const base = `${CRM_API}/agencies/${encodeURIComponent(tid)}`;

  const summary = useQuery({
    queryKey: key(tid, "summary"),
    queryFn: () => apiGet<TenantSummary>(base),
    initialData: agency,
    initialDataUpdatedAt: 0,
    retry: false,
  });
  const a = summary.data ?? agency;
  const usage = useQuery({ queryKey: key(tid, "usage"), queryFn: () => apiGet<AgencyUsage>(`${base}/usage`), retry: false });
  const ent = useQuery({
    queryKey: key(tid, "entitlements"), queryFn: () => apiGet<AgencyEntitlements>(`${base}/entitlements`), retry: false,
  });
  const requests = useQuery({
    queryKey: key(tid, "data-requests"),
    queryFn: async () => (await apiGet<DataRequestList>(`${base}/data-requests`)).items,
    retry: false,
  });
  const members = useQuery({
    queryKey: key(tid, "members"), queryFn: () => apiGet<AgencyMembers>(`${base}/members`), retry: false,
  });
  const jobs = useQuery({
    queryKey: key(tid, "jobs"),
    queryFn: async () => (await apiGet<FailedJobList>(`${base}/jobs`)).items,
    retry: false,
  });

  const refreshAll = () => qc.invalidateQueries({ queryKey: ["crm-agency", tid] });
  const fetching = [summary, usage, ent, requests, members, jobs].some((q) => q.isFetching);

  /** After a successful write: toast, refresh this agency and the list. */
  const done = (message: string, next?: TenantSummary) => {
    toast.success(message);
    if (next?.tenant_id) qc.setQueryData(key(tid, "summary"), next);
    void refreshAll();
    onChanged(next?.tenant_id ? next : undefined);
  };

  const st = stateLabel(a.state);
  const active = a.state === "active";
  const lockable = a.state === "active" || a.state === "locked";

  return (
    <Drawer open onClose={onClose} width={640}
      title={
        <span style={{ display: "inline-flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          {a.agency_name} <Badge tone={st.tone}>{st.label}</Badge>
          {a.is_synthetic && <Badge tone="purple" dot={false}>Synthetic</Badge>}
        </span>
      }
      footer={
        <Button variant="secondary" size="sm" onClick={() => void refreshAll()} disabled={fetching}>
          <RefreshCw size={13} /> {fetching ? "Loading…" : "Refresh"}
        </Button>
      }>
      <SkeletonStyle />
      <div data-testid="crm-agency-drawer" style={{ display: "flex", flexDirection: "column", gap: 14, fontFamily: sans }}>
        <div style={{ ...noteStyle, fontFamily: mono }}>Agency id {tid}</div>
        {summary.isError && <InlineError message={errText(summary.error)} />}
        {a.state === "locked" && (
          <div style={{ ...noteStyle, color: K.amber }}>
            Locked: members cannot sign in to the CRM, new inquiries are refused, and owner recovery, first-owner invites
            and data requests wait until the agency is unlocked. A pending owner recovery&apos;s 14 days keep running.
          </div>
        )}
        {!canWrite && (
          <div style={noteStyle}>Read only: your staff account is not in the crm-platform-admin group.</div>
        )}

        <UsageSection q={usage} />
        <AccessSection agency={a} canWrite={canWrite && lockable} base={base} onDone={done} />
        <FirstOwnerSection agency={a} canWrite={canWrite} base={base} onDone={done} />
        <FeaturesSection q={ent} canWrite={canWrite && lockable} base={base} onDone={done} />
        <InsightsSection q={ent} canWrite={canWrite && lockable} base={base} onDone={done} />
        <DataRequestsSection q={requests} canWrite={canWrite} active={active} base={base} onDone={done} />
        <RecoverySection q={members} canWrite={canWrite} active={active} base={base} onDone={done} />
        <JobsSection q={jobs} canWrite={canWrite} locked={a.state === "locked"} base={base} onDone={done} />
      </div>
    </Drawer>
  );
}

type Q<T> = { data?: T; isLoading: boolean; isError: boolean; error: unknown };
type Done = (message: string, next?: TenantSummary) => void;

function Loading() {
  return <SkeletonBar w="100%" h={48} />;
}

// ── 1. Usage meters ───────────────────────────────────────────────────────────────────────────────

function Tile({ label, value, sub, children }: { label: string; value: React.ReactNode; sub?: React.ReactNode; children?: React.ReactNode }) {
  return (
    <div style={{ border: `1px solid ${K.line}`, borderRadius: RADIUS.md, padding: "8px 10px", minWidth: 0 }}>
      <div style={{ fontSize: 11.5, color: K.muted }}>{label}</div>
      <div style={{ fontSize: 16, fontWeight: 700, color: K.ink, marginTop: 2 }}>{value}</div>
      {sub && <div style={{ fontSize: 11.5, color: K.muted, marginTop: 2 }}>{sub}</div>}
      {children}
    </div>
  );
}

function Bar({ value, warn }: { value: number; warn: boolean }) {
  return (
    <div aria-hidden style={{ height: 6, borderRadius: 3, background: K.line, marginTop: 6, overflow: "hidden" }}>
      <div style={{ width: `${value}%`, height: "100%", background: warn ? K.amber : K.accent }} />
    </div>
  );
}

function UsageSection({ q }: { q: Q<AgencyUsage> }) {
  const u = q.data;
  return (
    <Section title="Usage" testId="crm-usage"
      aside={u ? <span style={noteStyle}>{u.period_start?.slice(0, 10)} to {u.period_end?.slice(0, 10)} (UTC)</span> : undefined}>
      {q.isLoading ? <Loading /> : q.isError ? <InlineError message={errText(q.error)} /> : u ? (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 140px), 1fr))", gap: 8 }}>
          <Tile label="Seats" value={`${u.seats_used} / ${u.seats_allowed}`}>
            <Bar value={pct(u.seats_used, u.seats_allowed)} warn={u.seats_used >= u.seats_allowed} />
          </Tile>
          <Tile label="Active members (30 days)" value={u.active_members_30d} />
          <Tile label="Storage" value={formatBytes(u.storage_bytes)} />
          <Tile label="AI spend this month" value={formatUsd(u.ai_cost_usd_month)} sub={`of ${formatUsd(u.ai_budget_usd_month)} budget`}>
            <Bar value={usdPct(u.ai_cost_usd_month, u.ai_budget_usd_month)} warn={usdPct(u.ai_cost_usd_month, u.ai_budget_usd_month) >= 80} />
          </Tile>
          <Tile label="Failed jobs (24 h)" value={<span style={{ color: u.failed_jobs_24h ? K.danger : K.ink }}>{u.failed_jobs_24h}</span>} />
          <Tile label="Inbound mail (30 days)" value={u.inbound_mail_30d || "—"}
            sub={u.inbound_mail_30d ? undefined : "Not tracked yet"} />
          <Tile label="Last activity" value={<span style={{ fontSize: 13 }}>{u.last_activity_at ? formatDateTime(u.last_activity_at) : "None"}</span>} />
        </div>
      ) : null}
    </Section>
  );
}

// ── 2. Access: lock / unlock ──────────────────────────────────────────────────────────────────────

function AccessSection({ agency, canWrite, base, onDone }: { agency: TenantSummary; canWrite: boolean; base: string; onDone: Done }) {
  const [open, setOpen] = useState<"lock" | "unlock" | null>(null);
  const [reason, setReason] = useState<string>(LOCK_REASONS[0].code);
  const locked = agency.state === "locked";
  const lockedBy = lockSourceText(agency);
  const feed = membershipFeed(agency);
  const path = base.slice(CRM_API.length);
  return (
    <Section title="Access" testId="crm-access">
      <div style={noteStyle}>
        {agency.state === "active" && "The agency is active."}
        {(agency.state === "offboarding" || agency.state === "offboarded") &&
          "The agency is being or has been offboarded; it cannot be locked or unlocked."}
      </div>
      {lockedBy && (
        <div data-testid="crm-lock-source" style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <span style={{ display: "inline-flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            <Badge tone="warning">{lockedBy.label}</Badge>
            {lockedBy.code && <span style={{ ...noteStyle, fontFamily: mono }}>{lockedBy.code}</span>}
          </span>
          {lockedBy.detail && <div style={noteStyle}>{lockedBy.detail}</div>}
        </div>
      )}
      {locked && (
        <div data-testid="crm-membership-feed" style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <span style={{ display: "inline-flex", alignItems: "center", gap: 8, flexWrap: "wrap", fontSize: 12.5, color: K.ink }}>
            CIS membership feed (all agencies) <Badge tone={feed.tone}>{feed.label}</Badge>
            <span style={noteStyle}>last successful pull: {feed.lastPull}</span>
          </span>
          <div style={noteStyle}>
            {feed.detail} A CIS lock, unlock or owner removal reaches the CRM within this lag at most.
          </div>
        </div>
      )}
      {canWrite && (
        <div>
          {locked ? (
            <Button size="sm" variant="secondary" onClick={() => setOpen("unlock")}><Unlock size={13} /> Unlock</Button>
          ) : (
            <Button size="sm" variant="danger" onClick={() => setOpen("lock")}><Lock size={13} /> Lock</Button>
          )}
        </div>
      )}
      <WriteModal<TenantSummary> open={open === "lock"} onClose={() => setOpen(null)} destructive
        title={`Lock ${agency.agency_name}?`} confirmLabel="Lock agency"
        submit={(ticket) => crmWrite<TenantSummary>("POST", `${path}/lock`, { ticket_ref: ticket, reason_code: reason })}
        onDone={(r) => { setOpen(null); onDone("Agency locked.", r.body); }}>
        <div style={noteStyle}>
          Members can no longer use the CRM and new inquiries are refused until AA unlocks the agency. Queued jobs of the
          agency fail and can be retried after the unlock.
        </div>
        <Field label="Reason" htmlFor="crm-lock-reason">
          <select id="crm-lock-reason" value={reason} onChange={(e) => setReason(e.target.value)} style={fieldStyle}>
            {LOCK_REASONS.map((r) => <option key={r.code} value={r.code}>{r.label}</option>)}
          </select>
        </Field>
      </WriteModal>
      <WriteModal<TenantSummary> open={open === "unlock"} onClose={() => setOpen(null)}
        title={`Unlock ${agency.agency_name}?`} confirmLabel="Unlock agency"
        submit={(ticket) => crmWrite<TenantSummary>("POST", `${path}/unlock`, { ticket_ref: ticket } satisfies TicketBody)}
        onDone={(r) => { setOpen(null); onDone("Agency unlocked.", r.body); }}>
        <div style={noteStyle}>Members can use the CRM again. Failed jobs can then be retried below.</div>
      </WriteModal>
    </Section>
  );
}

// ── 3. First owner ────────────────────────────────────────────────────────────────────────────────

function FirstOwnerSection({ agency, canWrite, base, onDone }: { agency: TenantSummary; canWrite: boolean; base: string; onDone: Done }) {
  const [open, setOpen] = useState(false);
  const [email, setEmail] = useState("");
  const path = base.slice(CRM_API.length);
  const blocked = agency.has_owner
    ? "The agency already has an owner (owner_exists)."
    : agency.holds_travellers
      ? "The agency already holds travellers or inquiries, so AA cannot invite a first owner (has_travellers)."
      : agency.state !== "active"
        ? "Only an active agency can get a first-owner invitation."
        : null;
  const emailOk = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email.trim());
  const close = () => { setEmail(""); setOpen(false); };
  return (
    <Section title="First owner" testId="crm-first-owner">
      <div style={noteStyle}>
        AA invites the first owner of an empty agency only. Later owners are invited by the agency itself. The address
        is sent to the CRM once and is not shown or kept here.
      </div>
      {blocked && <div style={{ ...noteStyle, color: K.ink3 }}>{blocked}</div>}
      {canWrite && (
        <div>
          <Button size="sm" variant="secondary" disabled={!!blocked} onClick={() => setOpen(true)}
            title={blocked ?? undefined}>Invite first owner</Button>
        </div>
      )}
      <WriteModal open={open} onClose={close} title="Invite the first owner" confirmLabel="Send invitation"
        canSubmit={emailOk}
        submit={(ticket) => crmWrite<OwnerInviteAccepted>("POST", `${path}/owner-invite`, { owner_email: email.trim(), ticket_ref: ticket })}
        onDone={(r) => {
          const expires = typeof r.body?.expires_at === "string" ? ` It expires ${formatDateTime(r.body.expires_at)}.` : "";
          close();
          onDone(`Invitation sent.${expires}`);
        }}>
        <Field label="Owner's email" htmlFor="crm-owner-email"
          hint={email && !emailOk ? "Enter a full email address." : "The CRM mails the invitation link to this address."}>
          <input id="crm-owner-email" type="email" autoComplete="off" value={email} maxLength={254}
            onChange={(e) => setEmail(e.target.value)} style={fieldStyle} />
        </Field>
      </WriteModal>
    </Section>
  );
}

// ── 4. Features allowed ───────────────────────────────────────────────────────────────────────────

function FeaturesSection({ q, canWrite, base, onDone }: { q: Q<AgencyEntitlements>; canWrite: boolean; base: string; onDone: Done }) {
  const [draft, setDraft] = useState<string[] | null>(null);
  const [open, setOpen] = useState(false);
  const current = q.data?.features_allowed ?? [];
  const chosen = draft ?? current;
  const dirty = draft !== null && (draft.length !== current.length || draft.some((f) => !current.includes(f)));
  const path = base.slice(CRM_API.length);
  const toggle = (code: string) =>
    setDraft((prev) => {
      const list = prev ?? current;
      return list.includes(code) ? list.filter((c) => c !== code) : [...list, code];
    });
  return (
    <Section title="Features allowed" testId="crm-features"
      aside={q.data ? <span style={noteStyle}>Plan {q.data.plan} · {q.data.seats_allowed} seats</span> : undefined}>
      <div style={noteStyle}>AA allows; the owner switches on. Allowing a feature does not turn it on for the agency.</div>
      {q.isLoading ? <Loading /> : q.isError ? <InlineError message={errText(q.error)} /> : (
        <>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 200px), 1fr))", gap: 6 }}>
            {FEATURES.map((f) => (
              <label key={f.code} style={{ display: "inline-flex", gap: 8, alignItems: "center", fontSize: 13 }}>
                <input type="checkbox" checked={chosen.includes(f.code)} disabled={!canWrite}
                  onChange={() => toggle(f.code)} />
                {f.label}
              </label>
            ))}
          </div>
          {canWrite && (
            <div style={{ display: "flex", gap: 8 }}>
              <Button size="sm" variant="secondary" disabled={!dirty} onClick={() => setOpen(true)}>Save allowed features</Button>
              {dirty && <Button size="sm" variant="ghost" onClick={() => setDraft(null)}>Undo</Button>}
            </div>
          )}
        </>
      )}
      <WriteModal open={open} onClose={() => setOpen(false)} title="Change the allowed features" confirmLabel="Save"
        submit={(ticket) => crmWrite("PUT", `${path}/entitlements-allowed`, { features_allowed: chosen, ticket_ref: ticket })}
        onDone={() => { setOpen(false); setDraft(null); onDone("Allowed features saved."); }}>
        <div style={noteStyle}>
          Allowed after saving: {chosen.length ? chosen.map((c) => FEATURES.find((f) => f.code === c)?.label ?? c).join(", ") : "none"}.
        </div>
      </WriteModal>
    </Section>
  );
}

// ── 5. AA insights opt-out ────────────────────────────────────────────────────────────────────────

function InsightsSection({ q, canWrite, base, onDone }: { q: Q<AgencyEntitlements>; canWrite: boolean; base: string; onDone: Done }) {
  const [open, setOpen] = useState(false);
  const optedOut = !!q.data?.insights_opt_out;
  const path = base.slice(CRM_API.length);
  return (
    <Section title="AA insights" testId="crm-insights"
      aside={q.data ? <Badge tone={optedOut ? "neutral" : "success"}>{optedOut ? "Opted out" : "Included"}</Badge> : undefined}>
      <div style={noteStyle}>
        Whether the agency&apos;s anonymous figures count in AA&apos;s cross-agency insights. Change it only when the owner
        asked in writing; the request reference goes into the agency&apos;s access log.
      </div>
      {q.isError && <InlineError message={errText(q.error)} />}
      {canWrite && q.data && (
        <div>
          <Button size="sm" variant="secondary" onClick={() => setOpen(true)}>
            {optedOut ? "Include again" : "Opt out"}
          </Button>
        </div>
      )}
      <WriteModal<{ opt_out?: boolean }> open={open} onClose={() => setOpen(false)}
        title={optedOut ? "Include the agency in AA insights again?" : "Opt the agency out of AA insights?"}
        confirmLabel={optedOut ? "Include" : "Opt out"} ticketLabel="Owner's request reference"
        ticketHint="The ticket holding the owner's written request, for example SUP-1024."
        submit={(ref) => crmWrite<InsightsOptOut>("PUT", `${path}/insights-opt-out`, { opt_out: !optedOut, owner_request_ref: ref } satisfies InsightsOptOutBody)}
        onDone={(r) => { setOpen(false); onDone(r.body?.opt_out ? "The agency is opted out of AA insights." : "The agency is included in AA insights."); }} />
    </Section>
  );
}

// ── 6. Data requests ──────────────────────────────────────────────────────────────────────────────

function DataRequestsSection({ q, canWrite, active, base, onDone }: {
  q: Q<DataRequest[]>; canWrite: boolean; active: boolean; base: string; onDone: Done;
}) {
  const [open, setOpen] = useState(false);
  const [kind, setKind] = useState<DataRequestKind>("agency_export");
  const path = base.slice(CRM_API.length);
  const rows = q.data ?? [];
  const openOne = rows.find((r) => r.status === "awaiting_owner_confirmation" || r.status === "confirmed");
  return (
    <Section title="Data requests" testId="crm-data-requests">
      <div style={noteStyle}>
        AA can raise an export or a deletion request; the agency&apos;s owner confirms or declines it in the CRM (Settings ›
        Data and privacy). AA never confirms or runs it.
      </div>
      {q.isLoading ? <Loading /> : q.isError ? <InlineError message={errText(q.error)} /> : rows.length === 0 ? (
        <div style={noteStyle}>No requests.</div>
      ) : (
        <ul style={{ margin: 0, padding: 0, listStyle: "none", display: "flex", flexDirection: "column", gap: 6 }}>
          {rows.map((r) => {
            const s = dataRequestStatus(r.status);
            return (
              <li key={r.request_id} style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", fontSize: 13 }}>
                <span style={{ color: K.ink }}>{kindLabel(r.kind)}</span>
                <Badge tone={s.tone}>{s.label}</Badge>
                <span style={noteStyle}>{r.raised_at ? formatDateTime(r.raised_at) : ""}</span>
              </li>
            );
          })}
        </ul>
      )}
      {canWrite && (
        <div>
          <Button size="sm" variant="secondary" disabled={!active || !!openOne} onClick={() => setOpen(true)}
            title={!active ? "Only an active agency (the owner must be able to confirm)" : openOne ? "A request is already open" : undefined}>
            Raise a request
          </Button>
        </div>
      )}
      <WriteModal open={open} onClose={() => setOpen(false)} title="Raise a data request" confirmLabel="Raise request"
        destructive={kind === "agency_deletion"}
        submit={(ticket) => crmWrite<DataRequestRaised>("POST", `${path}/data-requests`, { kind, ticket_ref: ticket })}
        onDone={() => { setOpen(false); onDone("Request raised. It waits for the owner's confirmation."); }}>
        <Field label="Request" htmlFor="crm-dr-kind">
          <select id="crm-dr-kind" value={kind} onChange={(e) => setKind(e.target.value as DataRequestKind)} style={fieldStyle}>
            {DATA_REQUEST_KINDS.map((k) => <option key={k.code} value={k.code}>{k.label}</option>)}
          </select>
        </Field>
        <div style={noteStyle}>
          The owners get an e-mail. Nothing happens until an owner confirms in the CRM; a deletion then starts with an
          export the owner can download.
        </div>
      </WriteModal>
    </Section>
  );
}

// ── 7. Owner recovery (and disabling a member) ────────────────────────────────────────────────────

function RecoverySection({ q, canWrite, active, base, onDone }: {
  q: Q<AgencyMembers>; canWrite: boolean; active: boolean; base: string; onDone: Done;
}) {
  const [openStart, setOpenStart] = useState(false);
  const [openComplete, setOpenComplete] = useState(false);
  const [disable, setDisable] = useState<string | null>(null);
  const [candidate, setCandidate] = useState("");
  const [reason, setReason] = useState<string>(RECOVERY_REASONS[0].code);
  const path = base.slice(CRM_API.length);
  const data = q.data;
  const pending = data?.recovery ?? null;
  const candidates = (data?.members ?? []).filter((m) => m.status === "active" && m.role !== "owner");
  const due = recoveryDue(pending?.eligible_at);
  const memberColumns = useMemo<ColumnDef<AgencyMember, unknown>[]>(() => [
    {
      accessorKey: "member_id", header: "Member",
      cell: (c) => <span style={{ fontFamily: mono }} title={c.row.original.member_id}>{shortId(c.row.original.member_id)}</span>,
    },
    { accessorKey: "role", header: "Role" },
    { accessorKey: "status", header: "Status" },
    {
      accessorKey: "joined_at", header: "Joined",
      cell: (c) => (c.row.original.joined_at ? formatDateTime(c.row.original.joined_at) : "—"),
    },
    ...(canWrite ? [{
      id: "actions", header: "", enableSorting: false,
      cell: (c: { row: { original: AgencyMember } }) => (c.row.original.status === "active" ? (
        <Button size="sm" variant="ghost" disabled={!active} onClick={() => setDisable(c.row.original.member_id)}>Disable</Button>
      ) : null),
    } as ColumnDef<AgencyMember, unknown>] : []),
  ], [canWrite, active]);
  return (
    <Section title="Owner recovery" testId="crm-recovery"
      aside={data ? <span style={noteStyle}>{data.active_owners} active owner{data.active_owners === 1 ? "" : "s"}</span> : undefined}>
      <div style={noteStyle}>
        When the agency has lost its owner, AA can promote an active member. Every member is told and can cancel within
        14 days; only then can AA complete it.
      </div>
      {q.isLoading ? <Loading /> : q.isError ? <InlineError message={errText(q.error)} /> : data ? (
        <>
          <DataTable<AgencyMember>
            tableId="crm-agency-members"
            data={data.members}
            columns={memberColumns}
            getRowId={(m) => m.member_id}
            searchable={false}
            paginate={false}
            emptyTitle="No members"
            tableMinWidth={420}
          />
          {pending ? (
            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", fontSize: 13 }}>
              <Badge tone="warning">Recovery pending</Badge>
              <span style={noteStyle}>can complete from {formatDateTime(pending.eligible_at)}</span>
              {canWrite && (
                <Button size="sm" variant="primary" disabled={!due || !active} onClick={() => setOpenComplete(true)}
                  title={!due ? "Members can still cancel until then" : undefined}>Complete</Button>
              )}
            </div>
          ) : canWrite && (
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "flex-end" }}>
              <Button size="sm" variant="secondary" disabled={!active || candidates.length === 0}
                title={candidates.length === 0 ? "No active member who is not an owner" : undefined}
                onClick={() => { setCandidate(candidates[0]?.member_id ?? ""); setOpenStart(true); }}>
                Start owner recovery
              </Button>
            </div>
          )}
        </>
      ) : null}
      <WriteModal open={openStart} onClose={() => setOpenStart(false)} title="Start an owner recovery"
        confirmLabel="Start recovery" canSubmit={!!candidate}
        submit={(ticket) => crmWrite<RecoveryOpened>("POST", `${path}/owner-recovery`, { candidate_member_id: candidate, reason, ticket_ref: ticket })}
        onDone={(r) => {
          setOpenStart(false);
          const when = typeof r.body?.eligible_at === "string" ? formatDateTime(r.body.eligible_at) : "in 14 days";
          onDone(`Recovery started. It can be completed from ${when} unless a member cancels it.`);
        }}>
        <Field label="Member to promote to owner" htmlFor="crm-rec-candidate">
          <select id="crm-rec-candidate" value={candidate} onChange={(e) => setCandidate(e.target.value)} style={fieldStyle}>
            {candidates.map((m) => <option key={m.member_id} value={m.member_id}>{shortId(m.member_id)} ({m.role})</option>)}
          </select>
        </Field>
        <Field label="Reason" htmlFor="crm-rec-reason">
          <select id="crm-rec-reason" value={reason} onChange={(e) => setReason(e.target.value)} style={fieldStyle}>
            {RECOVERY_REASONS.map((r) => <option key={r.code} value={r.code}>{r.label}</option>)}
          </select>
        </Field>
      </WriteModal>
      <WriteModal open={openComplete} onClose={() => setOpenComplete(false)} title="Complete the owner recovery"
        confirmLabel="Promote to owner"
        submit={(ticket) => pending
          ? crmWrite<RecoveryCompleted>("POST", `${path}/owner-recovery/${encodeURIComponent(pending.request_id)}/complete`, { ticket_ref: ticket } satisfies TicketBody)
          : Promise.resolve<WriteResult<RecoveryCompleted>>({ ok: false, status: 0, message: "No pending recovery.", code: null })}
        onDone={() => { setOpenComplete(false); onDone("The candidate is now an owner."); }}>
        <div style={noteStyle}>The 14 days have passed and no member cancelled. The candidate becomes an owner.</div>
      </WriteModal>
      <WriteModal open={disable !== null} onClose={() => setDisable(null)} destructive
        title={`Disable member ${shortId(disable)}?`} confirmLabel="Disable member"
        submit={(ticket) => crmWrite<MemberDeactivated>("POST", `${path}/members/${encodeURIComponent(disable ?? "")}/deactivate`, { ticket_ref: ticket } satisfies TicketBody)}
        onDone={() => { setDisable(null); onDone("Member disabled and signed out."); }}>
        <div style={noteStyle}>
          The member can no longer sign in. The last active owner cannot be disabled here: use an owner recovery.
        </div>
      </WriteModal>
    </Section>
  );
}

// ── 8. Failed jobs ────────────────────────────────────────────────────────────────────────────────

function JobsSection({ q, canWrite, locked, base, onDone }: {
  q: Q<FailedJob[]>; canWrite: boolean; locked: boolean; base: string; onDone: Done;
}) {
  const [retry, setRetry] = useState<FailedJob | null>(null);
  const path = base.slice(CRM_API.length);
  const rows = q.data ?? [];
  return (
    <Section title="Failed jobs (7 days)" testId="crm-jobs">
      {q.isLoading ? <Loading /> : q.isError ? <InlineError message={errText(q.error)} /> : rows.length === 0 ? (
        <div style={noteStyle}>No failed jobs.</div>
      ) : (
        <ul style={{ margin: 0, padding: 0, listStyle: "none", display: "flex", flexDirection: "column", gap: 6 }}>
          {rows.map((j) => (
            <li key={j.job_id} style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", fontSize: 13 }}>
              <span style={{ fontFamily: mono }} title={j.job_id}>{shortId(j.job_id)}</span>
              <span style={{ color: K.ink }}>{j.kind}</span>
              <span style={noteStyle}>
                attempt {j.attempt}/{j.max_attempts}{j.error_class ? ` · ${j.error_class}` : ""}
                {j.finished_at ? ` · ${formatDateTime(j.finished_at)}` : ""}
              </span>
              {canWrite && (
                <Button size="sm" variant="ghost" disabled={locked} onClick={() => setRetry(j)}
                  title={locked ? "Unlock the agency first: the job would fail again" : undefined}>Retry</Button>
              )}
            </li>
          ))}
        </ul>
      )}
      <WriteModal open={retry !== null} onClose={() => setRetry(null)} title={`Retry job ${shortId(retry?.job_id)}?`}
        confirmLabel="Retry job" destructive={retry?.kind === "crm_offboard"}
        submit={(ticket) => crmWrite<JobRetried>("POST", `${path}/jobs/${encodeURIComponent(retry?.job_id ?? "")}/retry`, { ticket_ref: ticket } satisfies TicketBody)}
        onDone={() => { setRetry(null); onDone("Job queued again."); }}>
        {retry?.kind === "crm_offboard" ? (
          <div style={{ ...noteStyle, color: K.danger, display: "flex", gap: 6 }}>
            <AlertTriangle size={14} style={{ flexShrink: 0, marginTop: 2 }} />
            This continues an export or erase the owner confirmed. An erase cannot be undone.
          </div>
        ) : (
          <div style={noteStyle}>The job runs again from its first attempt.</div>
        )}
      </WriteModal>
    </Section>
  );
}
