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
// S263 (crm.ops.v1 1.8): withdraw the first-owner invitation AA sent (pending, or claimed and never accepted), so a new
// one can go; delete a TEST (synthetic) agency with no member and no traveller (typed name + ticket; the section is
// shown for synthetic agencies only and says why it is disabled). A real agency is deleted only by its owner.

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
  mono,
  sans,
  useFormat,
  useT,
  useToast,
  type ColumnDef,
  Checkbox, Input, Select,
} from "../../../_kit";
import {
  CRM_API,
  LOCK_REASONS,
  RECOVERY_REASONS,
  agencyNameMatches,
  formatBytes,
  formatUsd,
  pct,
  recoveryDue,
  shortId,
  testAgencyDeleteBlock,
  usdPct,
  type AgencyDeleted,
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
  type OwnerInviteWithdrawn,
  type RecoveryCompleted,
  type RecoveryOpened,
  type TenantSummary,
  type TicketBody,
  type WriteResult,
} from "@/lib/crmConsole";
import { Field, InlineError, Section, WriteModal, fieldStyle, noteStyle } from "./consoleUi";
import { DRAWER } from "./messages";
import { useCrmText } from "./useCrmText";

type Props = {
  agency: TenantSummary;
  canWrite: boolean;
  onClose: () => void;
  /** After a write: the new summary when the CRM returned one (lock / unlock), else undefined. */
  onChanged: (next?: TenantSummary) => void;
};

const key = (tid: string, part: string) => ["crm-agency", tid, part] as const;

function useErrText(): (e: unknown) => string {
  const t = useT(DRAWER);
  const crm = useCrmText();
  return (e: unknown) => {
    if (e instanceof ApiError) return crm.errorText(e.status, e.message);
    return (e as Error)?.message || t("loadError");
  };
}

export default function AgencyDrawer({ agency, canWrite, onClose, onChanged }: Props) {
  const t = useT(DRAWER);
  const crm = useCrmText();
  const errText = useErrText();
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

  const st = crm.state(a.state);
  const active = a.state === "active";
  const lockable = a.state === "active" || a.state === "locked";

  return (
    <Drawer open onClose={onClose} width={640}
      title={
        <span style={{ display: "inline-flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          {a.agency_name} <Badge tone={st.tone}>{st.label}</Badge>
          {a.is_synthetic && <Badge tone="purple" dot={false}>{t("synthetic")}</Badge>}
        </span>
      }
      footer={
        <Button variant="secondary" size="sm" onClick={() => void refreshAll()} disabled={fetching}>
          <RefreshCw size={13} /> {fetching ? t("loading") : t("refresh")}
        </Button>
      }>
      <SkeletonStyle />
      <div data-testid="crm-agency-drawer" style={{ display: "flex", flexDirection: "column", gap: 14, fontFamily: sans }}>
        <div style={{ ...noteStyle, fontFamily: mono }}>{t("agencyId", tid)}</div>
        {summary.isError && <InlineError message={errText(summary.error)} />}
        {a.state === "locked" && (
          <div style={{ ...noteStyle, color: K.amber }}>{t("lockedNote")}</div>
        )}
        {!canWrite && (
          <div style={noteStyle}>{t("readOnly")}</div>
        )}

        <UsageSection q={usage} />
        <AccessSection agency={a} canWrite={canWrite && lockable} base={base} onDone={done} />
        <FirstOwnerSection agency={a} canWrite={canWrite} base={base} onDone={done} />
        <FeaturesSection q={ent} canWrite={canWrite && lockable} base={base} onDone={done} />
        <InsightsSection q={ent} canWrite={canWrite && lockable} base={base} onDone={done} />
        <DataRequestsSection q={requests} canWrite={canWrite} active={active} base={base} onDone={done} />
        <RecoverySection q={members} canWrite={canWrite} active={active} base={base} onDone={done} />
        <JobsSection q={jobs} canWrite={canWrite} locked={a.state === "locked"} base={base} onDone={done} />
        {a.is_synthetic && (
          <DeleteTestAgencySection agency={a} members={members.data?.members} canWrite={canWrite} base={base}
            onDeleted={(message) => { toast.success(message); onChanged(); onClose(); }} />
        )}
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
  const t = useT(DRAWER);
  const { formatDateTime } = useFormat();
  const errText = useErrText();
  const u = q.data;
  return (
    <Section title={t("usage")} testId="crm-usage"
      aside={u ? <span style={noteStyle}>{t("period", u.period_start?.slice(0, 10) ?? "", u.period_end?.slice(0, 10) ?? "")}</span> : undefined}>
      {q.isLoading ? <Loading /> : q.isError ? <InlineError message={errText(q.error)} /> : u ? (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 140px), 1fr))", gap: 8 }}>
          <Tile label={t("seats")} value={`${u.seats_used} / ${u.seats_allowed}`}>
            <Bar value={pct(u.seats_used, u.seats_allowed)} warn={u.seats_used >= u.seats_allowed} />
          </Tile>
          <Tile label={t("activeMembers")} value={u.active_members_30d} />
          <Tile label={t("storage")} value={formatBytes(u.storage_bytes)} />
          <Tile label={t("aiSpend")} value={formatUsd(u.ai_cost_usd_month)} sub={t("ofBudget", formatUsd(u.ai_budget_usd_month))}>
            <Bar value={usdPct(u.ai_cost_usd_month, u.ai_budget_usd_month)} warn={usdPct(u.ai_cost_usd_month, u.ai_budget_usd_month) >= 80} />
          </Tile>
          <Tile label={t("failedJobs24")} value={<span style={{ color: u.failed_jobs_24h ? K.danger : K.ink }}>{u.failed_jobs_24h}</span>} />
          <Tile label={t("inboundMail")} value={u.inbound_mail_30d || "—"}
            sub={u.inbound_mail_30d ? undefined : t("notTracked")} />
          <Tile label={t("lastActivity")} value={<span style={{ fontSize: 13 }}>{u.last_activity_at ? formatDateTime(u.last_activity_at) : t("none")}</span>} />
        </div>
      ) : null}
    </Section>
  );
}

// ── 2. Access: lock / unlock ──────────────────────────────────────────────────────────────────────

function AccessSection({ agency, canWrite, base, onDone }: { agency: TenantSummary; canWrite: boolean; base: string; onDone: Done }) {
  const t = useT(DRAWER);
  const crm = useCrmText();
  const [open, setOpen] = useState<"lock" | "unlock" | null>(null);
  const [reason, setReason] = useState<string>(LOCK_REASONS[0].code);
  const locked = agency.state === "locked";
  const lockedBy = crm.lockSource(agency);
  const feed = crm.feed(agency);
  const path = base.slice(CRM_API.length);
  return (
    <Section title={t("access")} testId="crm-access">
      <div style={noteStyle}>
        {agency.state === "active" && t("isActive")}
        {(agency.state === "offboarding" || agency.state === "offboarded") && t("isOffboarded")}
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
            {t("feedTitle")} <Badge tone={feed.tone}>{feed.label}</Badge>
            <span style={noteStyle}>{t("lastPull", feed.lastPull)}</span>
          </span>
          <div style={noteStyle}>
            {feed.detail} {t("feedLag")}
          </div>
        </div>
      )}
      {canWrite && (
        <div>
          {locked ? (
            <Button size="sm" variant="secondary" onClick={() => setOpen("unlock")}><Unlock size={13} /> {t("unlock")}</Button>
          ) : (
            <Button size="sm" variant="danger" onClick={() => setOpen("lock")}><Lock size={13} /> {t("lock")}</Button>
          )}
        </div>
      )}
      <WriteModal<TenantSummary> open={open === "lock"} onClose={() => setOpen(null)} destructive
        title={t("lockTitle", agency.agency_name)} confirmLabel={t("lockConfirm")}
        submit={(ticket) => crm.write<TenantSummary>("POST", `${path}/lock`, { ticket_ref: ticket, reason_code: reason })}
        onDone={(r) => { setOpen(null); onDone(t("locked"), r.body); }}>
        <div style={noteStyle}>{t("lockNote")}</div>
        <Field label={t("reason")} htmlFor="crm-lock-reason">
          <Select id="crm-lock-reason" value={reason} onChange={(e) => setReason(e.target.value)} style={fieldStyle}>
            {crm.lockReasons.map((r) => <option key={r.code} value={r.code}>{r.label}</option>)}
          </Select>
        </Field>
      </WriteModal>
      <WriteModal<TenantSummary> open={open === "unlock"} onClose={() => setOpen(null)}
        title={t("unlockTitle", agency.agency_name)} confirmLabel={t("unlockConfirm")}
        submit={(ticket) => crm.write<TenantSummary>("POST", `${path}/unlock`, { ticket_ref: ticket } satisfies TicketBody)}
        onDone={(r) => { setOpen(null); onDone(t("unlocked"), r.body); }}>
        <div style={noteStyle}>{t("unlockNote")}</div>
      </WriteModal>
    </Section>
  );
}

// ── 3. First owner ────────────────────────────────────────────────────────────────────────────────

function FirstOwnerSection({ agency, canWrite, base, onDone }: { agency: TenantSummary; canWrite: boolean; base: string; onDone: Done }) {
  const t = useT(DRAWER);
  const crm = useCrmText();
  const { formatDateTime } = useFormat();
  const [open, setOpen] = useState(false);
  const [email, setEmail] = useState("");
  const path = base.slice(CRM_API.length);
  const blocked = agency.has_owner
    ? t("ownerExists")
    : agency.holds_travellers
      ? t("hasTravellers")
      : agency.state !== "active"
        ? t("onlyActive")
        : null;
  const emailOk = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email.trim());
  const close = () => { setEmail(""); setOpen(false); };
  const [withdrawing, setWithdrawing] = useState(false);
  // S263: an invitation sent but never accepted blocks a new one (owner_invite_pending); AA withdraws it first.
  const canWithdraw = !agency.has_owner && (agency.state === "active" || agency.state === "locked");
  return (
    <Section title={t("firstOwner")} testId="crm-first-owner">
      <div style={noteStyle}>{t("firstOwnerNote")}</div>
      {blocked && <div style={{ ...noteStyle, color: K.ink3 }}>{blocked}</div>}
      {canWrite && (
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <Button size="sm" variant="secondary" disabled={!!blocked} onClick={() => setOpen(true)}
            title={blocked ?? undefined}>{t("inviteFirst")}</Button>
          {canWithdraw && (
            <Button size="sm" variant="ghost" testId="crm-withdraw-owner-invite" onClick={() => setWithdrawing(true)}>
              {t("withdrawInvite")}
            </Button>
          )}
        </div>
      )}
      <WriteModal open={withdrawing} onClose={() => setWithdrawing(false)} title={t("withdrawTitle")}
        confirmLabel={t("withdrawConfirm")} destructive
        submit={(ticket) => crm.write<OwnerInviteWithdrawn>("POST", `${path}/owner-invite/withdraw`, { ticket_ref: ticket })}
        onDone={() => { setWithdrawing(false); onDone(t("withdrawn")); }}>
        <div style={noteStyle}>{t("withdrawNote")}</div>
      </WriteModal>
      <WriteModal open={open} onClose={close} title={t("inviteTitle")} confirmLabel={t("sendInvite")}
        canSubmit={emailOk}
        submit={(ticket) => crm.write<OwnerInviteAccepted>("POST", `${path}/owner-invite`, { owner_email: email.trim(), ticket_ref: ticket })}
        onDone={(r) => {
          const expires = typeof r.body?.expires_at === "string" ? t("expires", formatDateTime(r.body.expires_at)) : "";
          close();
          onDone(t("inviteSent", expires));
        }}>
        <Field label={t("ownerEmail")} htmlFor="crm-owner-email"
          hint={email && !emailOk ? t("emailBad") : t("emailHint")}>
          <Input id="crm-owner-email" type="email" autoComplete="off" value={email} maxLength={254}
            onChange={(e) => setEmail(e.target.value)} style={fieldStyle} />
        </Field>
      </WriteModal>
    </Section>
  );
}

// ── 3b. Delete a test agency (S263, synthetic agencies only) ──────────────────────────────────────

function DeleteTestAgencySection({ agency, members, canWrite, base, onDeleted }: {
  agency: TenantSummary;
  members: AgencyMember[] | undefined;
  canWrite: boolean;
  base: string;
  onDeleted: (message: string) => void;
}) {
  const t = useT(DRAWER);
  const crm = useCrmText();
  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState("");
  const path = base.slice(CRM_API.length);
  const block = testAgencyDeleteBlock(agency, members);
  const why = block ? t(`deleteBlocked_${block}`) : null;
  const close = () => { setTyped(""); setOpen(false); };
  const nameOk = agencyNameMatches(typed, agency.agency_name);
  return (
    <Section title={t("deleteSection")} testId="crm-delete-test-agency">
      <div style={noteStyle}>{t("deleteNote")}</div>
      {why && <div style={{ ...noteStyle, color: K.ink3 }}>{why}</div>}
      {canWrite && (
        <div>
          <Button size="sm" variant="danger" disabled={!!block} title={why ?? undefined}
            testId="crm-delete-test-agency-open" onClick={() => setOpen(true)}>{t("deleteButton")}</Button>
        </div>
      )}
      <WriteModal open={open} onClose={close} title={t("deleteTitle", agency.agency_name)} confirmLabel={t("deleteConfirm")}
        destructive canSubmit={nameOk}
        submit={(ticket) => crm.write<AgencyDeleted>("POST", `${path}/delete`, { agency_name: typed, ticket_ref: ticket })}
        onDone={() => { const name = agency.agency_name; close(); onDeleted(t("deleted", name)); }}>
        <div style={noteStyle}>{t("deleteNote")}</div>
        <Field label={t("deleteName")} htmlFor="crm-delete-name"
          hint={typed && !nameOk ? t("deleteNameBad") : t("deleteNameHint", agency.agency_name)}>
          <Input id="crm-delete-name" data-testid="crm-delete-name" type="text" autoComplete="off" value={typed}
            maxLength={200} onChange={(e) => setTyped(e.target.value)} style={fieldStyle} aria-invalid={!!typed && !nameOk} />
        </Field>
      </WriteModal>
    </Section>
  );
}

// ── 4. Features allowed ───────────────────────────────────────────────────────────────────────────

function FeaturesSection({ q, canWrite, base, onDone }: { q: Q<AgencyEntitlements>; canWrite: boolean; base: string; onDone: Done }) {
  const t = useT(DRAWER);
  const crm = useCrmText();
  const errText = useErrText();
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
    <Section title={t("features")} testId="crm-features"
      aside={q.data ? <span style={noteStyle}>{t("planSeats", q.data.plan, q.data.seats_allowed)}</span> : undefined}>
      <div style={noteStyle}>{t("featuresNote")}</div>
      {q.isLoading ? <Loading /> : q.isError ? <InlineError message={errText(q.error)} /> : (
        <>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 200px), 1fr))", gap: 6 }}>
            {crm.features.map((f) => (
              <label key={f.code} style={{ display: "inline-flex", gap: 8, alignItems: "center", fontSize: 13 }}>
                <Checkbox checked={chosen.includes(f.code)} disabled={!canWrite}
                  onChange={() => toggle(f.code)} />
                {f.label}
              </label>
            ))}
          </div>
          {canWrite && (
            <div style={{ display: "flex", gap: 8 }}>
              <Button size="sm" variant="secondary" disabled={!dirty} onClick={() => setOpen(true)}>{t("saveFeatures")}</Button>
              {dirty && <Button size="sm" variant="ghost" onClick={() => setDraft(null)}>{t("undo")}</Button>}
            </div>
          )}
        </>
      )}
      <WriteModal open={open} onClose={() => setOpen(false)} title={t("featuresTitle")} confirmLabel={t("save")}
        submit={(ticket) => crm.write("PUT", `${path}/entitlements-allowed`, { features_allowed: chosen, ticket_ref: ticket })}
        onDone={() => { setOpen(false); setDraft(null); onDone(t("featuresSaved")); }}>
        <div style={noteStyle}>
          {t("allowedAfter", chosen.length ? chosen.map((c) => crm.featureLabel(c)).join(", ") : t("noneLower"))}
        </div>
      </WriteModal>
    </Section>
  );
}

// ── 5. AA insights opt-out ────────────────────────────────────────────────────────────────────────

function InsightsSection({ q, canWrite, base, onDone }: { q: Q<AgencyEntitlements>; canWrite: boolean; base: string; onDone: Done }) {
  const t = useT(DRAWER);
  const crm = useCrmText();
  const errText = useErrText();
  const [open, setOpen] = useState(false);
  const optedOut = !!q.data?.insights_opt_out;
  const path = base.slice(CRM_API.length);
  return (
    <Section title={t("insights")} testId="crm-insights"
      aside={q.data ? <Badge tone={optedOut ? "neutral" : "success"}>{optedOut ? t("optedOut") : t("included")}</Badge> : undefined}>
      <div style={noteStyle}>{t("insightsNote")}</div>
      {q.isError && <InlineError message={errText(q.error)} />}
      {canWrite && q.data && (
        <div>
          <Button size="sm" variant="secondary" onClick={() => setOpen(true)}>
            {optedOut ? t("includeAgain") : t("optOut")}
          </Button>
        </div>
      )}
      <WriteModal<{ opt_out?: boolean }> open={open} onClose={() => setOpen(false)}
        title={optedOut ? t("includeTitle") : t("optOutTitle")}
        confirmLabel={optedOut ? t("include") : t("optOut")} ticketLabel={t("ownerRef")}
        ticketHint={t("ownerRefHint")}
        submit={(ref) => crm.write<InsightsOptOut>("PUT", `${path}/insights-opt-out`, { opt_out: !optedOut, owner_request_ref: ref } satisfies InsightsOptOutBody)}
        onDone={(r) => { setOpen(false); onDone(r.body?.opt_out ? t("nowOptedOut") : t("nowIncluded")); }} />
    </Section>
  );
}

// ── 6. Data requests ──────────────────────────────────────────────────────────────────────────────

function DataRequestsSection({ q, canWrite, active, base, onDone }: {
  q: Q<DataRequest[]>; canWrite: boolean; active: boolean; base: string; onDone: Done;
}) {
  const t = useT(DRAWER);
  const crm = useCrmText();
  const { formatDateTime } = useFormat();
  const errText = useErrText();
  const [open, setOpen] = useState(false);
  const [kind, setKind] = useState<DataRequestKind>("agency_export");
  const path = base.slice(CRM_API.length);
  const rows = q.data ?? [];
  const openOne = rows.find((r) => r.status === "awaiting_owner_confirmation" || r.status === "confirmed");
  return (
    <Section title={t("dataRequests")} testId="crm-data-requests">
      <div style={noteStyle}>{t("dataNote")}</div>
      {q.isLoading ? <Loading /> : q.isError ? <InlineError message={errText(q.error)} /> : rows.length === 0 ? (
        <div style={noteStyle}>{t("noRequests")}</div>
      ) : (
        <ul style={{ margin: 0, padding: 0, listStyle: "none", display: "flex", flexDirection: "column", gap: 6 }}>
          {rows.map((r) => {
            const s = crm.dataStatus(r.status);
            return (
              <li key={r.request_id} style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", fontSize: 13 }}>
                <span style={{ color: K.ink }}>{crm.kindLabel(r.kind)}</span>
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
            title={!active ? t("onlyActiveRequest") : openOne ? t("requestOpen") : undefined}>
            {t("raiseRequest")}
          </Button>
        </div>
      )}
      <WriteModal open={open} onClose={() => setOpen(false)} title={t("raiseTitle")} confirmLabel={t("raiseConfirm")}
        destructive={kind === "agency_deletion"}
        submit={(ticket) => crm.write<DataRequestRaised>("POST", `${path}/data-requests`, { kind, ticket_ref: ticket })}
        onDone={() => { setOpen(false); onDone(t("raised")); }}>
        <Field label={t("request")} htmlFor="crm-dr-kind">
          <Select id="crm-dr-kind" value={kind} onChange={(e) => setKind(e.target.value as DataRequestKind)} style={fieldStyle}>
            {crm.dataKinds.map((k) => <option key={k.code} value={k.code}>{k.label}</option>)}
          </Select>
        </Field>
        <div style={noteStyle}>{t("raiseNote")}</div>
      </WriteModal>
    </Section>
  );
}

// ── 7. Owner recovery (and disabling a member) ────────────────────────────────────────────────────

function RecoverySection({ q, canWrite, active, base, onDone }: {
  q: Q<AgencyMembers>; canWrite: boolean; active: boolean; base: string; onDone: Done;
}) {
  const t = useT(DRAWER);
  const crm = useCrmText();
  const { formatDateTime } = useFormat();
  const errText = useErrText();
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
      accessorKey: "member_id", header: t("colMember"),
      cell: (c) => <span style={{ fontFamily: mono }} title={c.row.original.member_id}>{shortId(c.row.original.member_id)}</span>,
    },
    { accessorKey: "role", header: t("colRole") },
    { accessorKey: "status", header: t("colStatus") },
    {
      accessorKey: "joined_at", header: t("colJoined"),
      cell: (c) => (c.row.original.joined_at ? formatDateTime(c.row.original.joined_at) : "—"),
    },
    ...(canWrite ? [{
      id: "actions", header: "", enableSorting: false,
      cell: (c: { row: { original: AgencyMember } }) => (c.row.original.status === "active" ? (
        <Button size="sm" variant="ghost" disabled={!active} onClick={() => setDisable(c.row.original.member_id)}>{t("disable")}</Button>
      ) : null),
    } as ColumnDef<AgencyMember, unknown>] : []),
  ], [canWrite, active, t, formatDateTime]);
  return (
    <Section title={t("recovery")} testId="crm-recovery"
      aside={data ? <span style={noteStyle}>{t("activeOwners", data.active_owners)}</span> : undefined}>
      <div style={noteStyle}>{t("recoveryNote")}</div>
      {q.isLoading ? <Loading /> : q.isError ? <InlineError message={errText(q.error)} /> : data ? (
        <>
          <DataTable<AgencyMember>
            tableId="crm-agency-members"
            data={data.members}
            columns={memberColumns}
            getRowId={(m) => m.member_id}
            searchable={false}
            paginate={false}
            emptyTitle={t("noMembers")}
            tableMinWidth={420}
          />
          {pending ? (
            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", fontSize: 13 }}>
              <Badge tone="warning">{t("recoveryPending")}</Badge>
              <span style={noteStyle}>{t("completeFrom", formatDateTime(pending.eligible_at))}</span>
              {canWrite && (
                <Button size="sm" variant="primary" disabled={!due || !active} onClick={() => setOpenComplete(true)}
                  title={!due ? t("stillCancel") : undefined}>{t("complete")}</Button>
              )}
            </div>
          ) : canWrite && (
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "flex-end" }}>
              <Button size="sm" variant="secondary" disabled={!active || candidates.length === 0}
                title={candidates.length === 0 ? t("noCandidate") : undefined}
                onClick={() => { setCandidate(candidates[0]?.member_id ?? ""); setOpenStart(true); }}>
                {t("startRecovery")}
              </Button>
            </div>
          )}
        </>
      ) : null}
      <WriteModal open={openStart} onClose={() => setOpenStart(false)} title={t("startTitle")}
        confirmLabel={t("startConfirm")} canSubmit={!!candidate}
        submit={(ticket) => crm.write<RecoveryOpened>("POST", `${path}/owner-recovery`, { candidate_member_id: candidate, reason, ticket_ref: ticket })}
        onDone={(r) => {
          setOpenStart(false);
          const when = typeof r.body?.eligible_at === "string" ? formatDateTime(r.body.eligible_at) : t("in14Days");
          onDone(t("recoveryStarted", when));
        }}>
        <Field label={t("promote")} htmlFor="crm-rec-candidate">
          <Select id="crm-rec-candidate" value={candidate} onChange={(e) => setCandidate(e.target.value)} style={fieldStyle}>
            {candidates.map((m) => <option key={m.member_id} value={m.member_id}>{shortId(m.member_id)} ({m.role})</option>)}
          </Select>
        </Field>
        <Field label={t("reason")} htmlFor="crm-rec-reason">
          <Select id="crm-rec-reason" value={reason} onChange={(e) => setReason(e.target.value)} style={fieldStyle}>
            {crm.recoveryReasons.map((r) => <option key={r.code} value={r.code}>{r.label}</option>)}
          </Select>
        </Field>
      </WriteModal>
      <WriteModal open={openComplete} onClose={() => setOpenComplete(false)} title={t("completeTitle")}
        confirmLabel={t("promoteConfirm")}
        submit={(ticket) => pending
          ? crm.write<RecoveryCompleted>("POST", `${path}/owner-recovery/${encodeURIComponent(pending.request_id)}/complete`, { ticket_ref: ticket } satisfies TicketBody)
          : Promise.resolve<WriteResult<RecoveryCompleted>>({ ok: false, status: 0, message: t("noPending"), code: null })}
        onDone={() => { setOpenComplete(false); onDone(t("promoted")); }}>
        <div style={noteStyle}>{t("completeNote")}</div>
      </WriteModal>
      <WriteModal open={disable !== null} onClose={() => setDisable(null)} destructive
        title={t("disableTitle", shortId(disable))} confirmLabel={t("disableConfirm")}
        submit={(ticket) => crm.write<MemberDeactivated>("POST", `${path}/members/${encodeURIComponent(disable ?? "")}/deactivate`, { ticket_ref: ticket } satisfies TicketBody)}
        onDone={() => { setDisable(null); onDone(t("disabled")); }}>
        <div style={noteStyle}>{t("disableNote")}</div>
      </WriteModal>
    </Section>
  );
}

// ── 8. Failed jobs ────────────────────────────────────────────────────────────────────────────────

function JobsSection({ q, canWrite, locked, base, onDone }: {
  q: Q<FailedJob[]>; canWrite: boolean; locked: boolean; base: string; onDone: Done;
}) {
  const t = useT(DRAWER);
  const crm = useCrmText();
  const { formatDateTime } = useFormat();
  const errText = useErrText();
  const [retry, setRetry] = useState<FailedJob | null>(null);
  const path = base.slice(CRM_API.length);
  const rows = q.data ?? [];
  return (
    <Section title={t("failedJobs7")} testId="crm-jobs">
      {q.isLoading ? <Loading /> : q.isError ? <InlineError message={errText(q.error)} /> : rows.length === 0 ? (
        <div style={noteStyle}>{t("noFailedJobs")}</div>
      ) : (
        <ul style={{ margin: 0, padding: 0, listStyle: "none", display: "flex", flexDirection: "column", gap: 6 }}>
          {rows.map((j) => (
            <li key={j.job_id} style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", fontSize: 13 }}>
              <span style={{ fontFamily: mono }} title={j.job_id}>{shortId(j.job_id)}</span>
              <span style={{ color: K.ink }}>{j.kind}</span>
              <span style={noteStyle}>
                {t("attempt", j.attempt, j.max_attempts)}{j.error_class ? ` · ${j.error_class}` : ""}
                {j.finished_at ? ` · ${formatDateTime(j.finished_at)}` : ""}
              </span>
              {canWrite && (
                <Button size="sm" variant="ghost" disabled={locked} onClick={() => setRetry(j)}
                  title={locked ? t("unlockFirst") : undefined}>{t("retry")}</Button>
              )}
            </li>
          ))}
        </ul>
      )}
      <WriteModal open={retry !== null} onClose={() => setRetry(null)} title={t("retryTitle", shortId(retry?.job_id))}
        confirmLabel={t("retryConfirm")} destructive={retry?.kind === "crm_offboard"}
        submit={(ticket) => crm.write<JobRetried>("POST", `${path}/jobs/${encodeURIComponent(retry?.job_id ?? "")}/retry`, { ticket_ref: ticket } satisfies TicketBody)}
        onDone={() => { setRetry(null); onDone(t("requeued")); }}>
        {retry?.kind === "crm_offboard" ? (
          <div style={{ ...noteStyle, color: K.danger, display: "flex", gap: 6 }}>
            <AlertTriangle size={14} style={{ flexShrink: 0, marginTop: 2 }} />
            {t("offboardWarn")}
          </div>
        ) : (
          <div style={noteStyle}>{t("retryNote")}</div>
        )}
      </WriteModal>
    </Section>
  );
}
