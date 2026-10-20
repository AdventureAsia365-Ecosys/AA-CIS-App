"use client";
// app/admin/platform/alerts/AlertRecipients.tsx
// AA-913 — Platform › People & access › Alert recipients: who gets the platform alert e-mails.
//
// Everyone on the list gets every platform alert: the SNS alerts topic (CloudWatch alarms of CIS, Booking,
// TripPlanner and shared; security alerts; cost anomalies; AWS Budgets) and the CIS spend e-mails (SES). Adding an
// address makes AWS send a confirmation e-mail (from no-reply@sns.amazonaws.com); the address receives alerts only
// after the link in it is clicked. The Terraform bootstrap recipient is shown locked and cannot be removed here.
//
// API (backend api/routers/admin_alert_recipients.py; BFF /api/admin/platform/*, admin only — lib/rbac.ts):
//   GET    /api/admin/platform/alert-recipients[?refresh=true]   list (the backend reconciles with SNS first)
//   POST   /api/admin/platform/alert-recipients                  {email, note?}
//   POST   /api/admin/platform/alert-recipients/refresh          check SNS now
//   POST   /api/admin/platform/alert-recipients/test-email       queue a test ops e-mail
//   DELETE /api/admin/platform/alert-recipients/{id}
//   POST   /api/admin/platform/alert-recipients/{id}/resend
// Changes need a company (Cognito) sign-in: the list's `viewer` block says whether this session may change it;
// the backend refuses everyone else anyway (403 with its reason, shown as-is).

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, BellRing, Lock, MailPlus, RefreshCw, Send, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import {
  Badge,
  Button,
  ConfirmModal,
  DataTable,
  ErrorState,
  K,
  Modal,
  PageHeader,
  RADIUS,
  apiGet,
  formatDateTime,
  mono,
  sans,
  useToast,
  type ColumnDef,
} from "../../../_kit";
import { staffErrorMessage } from "@/lib/rbac";
import {
  emailLooksValid,
  formatWait,
  isCompanyDomain,
  retryAfterSeconds,
  secondsUntil,
  snsStateLabel,
  statusLabel,
  statusTone,
  type AlertRecipient,
  type AlertRecipientsPage,
  type SnsState,
} from "@/lib/alertRecipients";

const API = "/api/admin/platform/alert-recipients";
const KEY = ["platform-alert-recipients"] as const;

type WriteResult = { ok: true; data: Record<string, unknown> } | { ok: false; message: string; retryAfter?: number };

/** One change through the BFF. Never throws. */
async function send(method: "POST" | "DELETE", path: string, body?: unknown): Promise<WriteResult> {
  let res: Response;
  try {
    res = await fetch(path, {
      method,
      headers: { "Content-Type": "application/json" },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    return { ok: false, message: "Network error: nothing was sent. Check your connection and try again." };
  }
  let data: Record<string, unknown> = {};
  try {
    data = (await res.json()) as Record<string, unknown>;
  } catch {
    /* non-JSON body */
  }
  if (res.ok) return { ok: true, data };
  let message: string;
  if (Array.isArray(data.detail)) {
    message = "Check the address and the note (the server refused the form).";
  } else {
    message = staffErrorMessage(res.status, data.detail, `Request failed (${res.status})`);
  }
  return { ok: false, message, retryAfter: retryAfterSeconds(data, res.headers.get("Retry-After")) };
}

function Notice({ tone, children }: { tone: "warning" | "info" | "danger"; children: React.ReactNode }) {
  const color = tone === "danger" ? K.danger : tone === "warning" ? K.amber : K.info;
  const bg = tone === "danger" ? K.dangerSoft : tone === "warning" ? K.amberSoft : K.infoSoft;
  return (
    <div
      role={tone === "info" ? "status" : "alert"}
      style={{
        display: "flex", gap: 10, alignItems: "flex-start", padding: "10px 14px", borderRadius: RADIUS.md,
        background: bg, color, fontSize: 13, lineHeight: 1.5, fontFamily: sans, marginBottom: 14,
      }}
    >
      <AlertTriangle size={16} style={{ flex: "none", marginTop: 2 }} />
      <div style={{ color: K.ink, minWidth: 0 }}>{children}</div>
    </div>
  );
}

function Section({ title, children, right }: { title: string; children: React.ReactNode; right?: React.ReactNode }) {
  return (
    <section
      style={{
        background: K.card, border: `1px solid ${K.line}`, borderRadius: RADIUS.lg, padding: "16px 18px",
        marginBottom: 16, minWidth: 0,
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8, flexWrap: "wrap", marginBottom: 10 }}>
        <h2 style={{ margin: 0, fontSize: 14, fontWeight: 700, color: K.ink, fontFamily: sans }}>{title}</h2>
        {right}
      </div>
      {children}
    </section>
  );
}

function LockedRow({ email, label, state }: { email: string; label: string; state?: SnsState | null }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", padding: "8px 0", borderTop: `1px solid ${K.line}` }}>
      <Lock size={14} style={{ color: K.muted, flex: "none" }} aria-label="Locked" />
      <span style={{ fontFamily: mono, fontSize: 13, color: K.ink, overflowWrap: "anywhere" }}>{email}</span>
      {state !== undefined && (
        <Badge tone={state === "confirmed" ? "success" : state === "pending" ? "warning" : "danger"} dot={false}>
          {snsStateLabel(state)}
        </Badge>
      )}
      <span style={{ fontSize: 12.5, color: K.muted }}>{label}</span>
    </div>
  );
}

function AddModal({ open, onClose, onAdded, limits, active }: {
  open: boolean;
  onClose: () => void;
  onAdded: (email: string, state: string) => void;
  limits: AlertRecipientsPage["limits"] | undefined;
  active: number;
}) {
  const [email, setEmail] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const maxNote = limits?.max_note ?? 80;
  const trimmed = email.trim();
  const valid = emailLooksValid(trimmed);
  const outside = valid && !isCompanyDomain(trimmed);
  const noteTooLong = note.trim().length > maxNote;

  const close = () => {
    if (busy) return;
    setEmail("");
    setNote("");
    setError(null);
    onClose();
  };

  const submit = async () => {
    if (!valid || noteTooLong || busy) return;
    setBusy(true);
    setError(null);
    const out = await send("POST", API, { email: trimmed, note: note.trim() || undefined });
    setBusy(false);
    if (!out.ok) {
      setError(out.message);
      return;
    }
    onAdded(trimmed.toLowerCase(), String(out.data.sns_state ?? "pending"));
    setEmail("");
    setNote("");
  };

  return (
    <Modal
      open={open}
      onClose={close}
      title="Add alert recipient"
      width={480}
      footer={
        <>
          <Button variant="ghost" onClick={close} disabled={busy}>Cancel</Button>
          <Button variant="primary" onClick={submit} disabled={!valid || noteTooLong || busy}>
            <MailPlus size={13} /> {busy ? "Adding…" : "Add and send confirmation"}
          </Button>
        </>
      }
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 12, fontFamily: sans }}>
        <p style={{ margin: 0, fontSize: 13, color: K.body, lineHeight: 1.5 }}>
          AWS sends a confirmation e-mail to this address (from <span style={{ fontFamily: mono }}>no-reply@sns.amazonaws.com</span>,
          subject &ldquo;AWS Notification - Subscription Confirmation&rdquo;). Alerts start only after the link in it is clicked;
          the link expires after 3 days. {limits ? `${active} of ${limits.max_active} recipients used.` : ""}
        </p>
        <label style={{ display: "flex", flexDirection: "column", gap: 5, fontSize: 12.5, fontWeight: 600, color: K.ink3 }}>
          E-mail address
          <input
            type="email"
            autoComplete="off"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") void submit(); }}
            placeholder="name@adventure.asia"
            aria-invalid={trimmed.length > 0 && !valid}
            style={inputStyle}
          />
        </label>
        {trimmed.length > 0 && !valid && (
          <div style={{ fontSize: 12.5, color: K.danger }}>
            One address only: no spaces, commas or angle brackets, and a domain with a dot.
          </div>
        )}
        {outside && (
          <Notice tone="warning">
            This address is outside {"adventure.asia"}. Alerts include security details (who read a secret, from which IP
            address). Add an outside mailbox only if you trust it.
          </Notice>
        )}
        <label style={{ display: "flex", flexDirection: "column", gap: 5, fontSize: 12.5, fontWeight: 600, color: K.ink3 }}>
          Note (optional)
          <input
            type="text"
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="e.g. on-call phone"
            style={inputStyle}
          />
          <span style={{ fontWeight: 400, color: noteTooLong ? K.danger : K.muted }}>
            {note.trim().length} / {maxNote}
          </span>
        </label>
        {error && <Notice tone="danger">{error}</Notice>}
      </div>
    </Modal>
  );
}

const inputStyle: React.CSSProperties = {
  padding: "8px 10px", borderRadius: RADIUS.md, border: `1px solid ${K.line}`, background: K.card, color: K.ink,
  fontSize: 13, fontFamily: sans, fontWeight: 400, minWidth: 0, width: "100%", boxSizing: "border-box",
};

export default function AlertRecipients() {
  const qc = useQueryClient();
  const toast = useToast();
  const list = useQuery({
    queryKey: KEY,
    queryFn: () => apiGet<AlertRecipientsPage>(API),
    staleTime: 30_000,
  });
  const data = list.data;
  const canWrite = !!data?.configured && !!data?.viewer?.can_write;

  // Resend countdowns: a coarse clock, ticking only while a row is waiting.
  const [now, setNow] = useState(() => Date.now());
  const waiting = useMemo(() => (data?.recipients ?? []).some((r) => r.status === "pending"), [data]);
  useEffect(() => {
    if (!waiting) return;
    const id = window.setInterval(() => setNow(Date.now()), 15_000);
    return () => window.clearInterval(id);
  }, [waiting]);
  // 429 Retry-After per row, from the last refused resend.
  const [blockedUntil, setBlockedUntil] = useState<Record<string, number>>({});

  const [adding, setAdding] = useState(false);
  const [removing, setRemoving] = useState<AlertRecipient | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [pageError, setPageError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const reload = () => qc.invalidateQueries({ queryKey: KEY });

  const refresh = async () => {
    setRefreshing(true);
    setPageError(null);
    const out = await send("POST", `${API}/refresh`);
    setRefreshing(false);
    if (!out.ok) {
      setPageError(out.message);
      return;
    }
    qc.setQueryData(KEY, out.data as unknown as AlertRecipientsPage);
  };

  const resend = async (r: AlertRecipient) => {
    setBusy(r.id);
    setPageError(null);
    const out = await send("POST", `${API}/${encodeURIComponent(r.id)}/resend`);
    setBusy(null);
    if (!out.ok) {
      if (out.retryAfter) setBlockedUntil((prev) => ({ ...prev, [r.id]: Date.now() + out.retryAfter! * 1000 }));
      setPageError(out.message);
      return;
    }
    toast.success(`AWS sent the confirmation e-mail to ${r.email} again. Ask them to check spam too.`, 6000);
    void reload();
  };

  const remove = async () => {
    const r = removing;
    if (!r) return;
    setBusy(r.id);
    setPageError(null);
    const out = await send("DELETE", `${API}/${encodeURIComponent(r.id)}`);
    setBusy(null);
    setRemoving(null);
    if (!out.ok) {
      setPageError(out.message);
      return;
    }
    toast.success(out.data.sns === "pending_left_to_expire"
      ? `Removed ${r.email}. Its unconfirmed AWS subscription expires on its own within 3 days; if the link is clicked first, the automatic status check (every 5 minutes) unsubscribes it again.`
      : `Removed ${r.email}. It gets no more alerts.`, 7000);
    void reload();
  };

  const sendTest = async () => {
    setBusy("test");
    setPageError(null);
    const out = await send("POST", `${API}/test-email`);
    setBusy(null);
    if (!out.ok) {
      setPageError(out.message);
      return;
    }
    toast.success(`Test e-mail queued for ${String(out.data.recipients_count ?? "")} recipient(s). It arrives in about a minute.`, 6000);
  };

  const columns = useMemo<ColumnDef<AlertRecipient, unknown>[]>(() => [
    {
      accessorKey: "email",
      header: "E-mail",
      cell: (c) => (
        <div style={{ minWidth: 0 }}>
          <div style={{ fontFamily: mono, fontSize: 13, color: K.ink, overflowWrap: "anywhere" }}>{c.row.original.email}</div>
          {c.row.original.note && <div style={{ fontSize: 12, color: K.muted }}>{c.row.original.note}</div>}
        </div>
      ),
    },
    {
      id: "status",
      accessorFn: (r) => r.status,
      header: "Status",
      cell: (c) => {
        const r = c.row.original;
        return (
          <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
            <Badge tone={statusTone(r)} dot={false}>{statusLabel(r)}</Badge>
            {r.status === "pending" && (
              <span style={{ fontSize: 11.5, color: K.muted, maxWidth: 240 }}>
                AWS sent a confirmation e-mail; the link expires after 3 days.
              </span>
            )}
          </div>
        );
      },
    },
    {
      id: "added",
      accessorFn: (r) => r.created_at ?? "",
      header: "Added",
      cell: (c) => (
        <div style={{ fontSize: 12.5 }}>
          <div>{c.row.original.created_at ? formatDateTime(c.row.original.created_at) : "—"}</div>
          <div style={{ color: K.muted }}>{c.row.original.created_by ?? ""}</div>
        </div>
      ),
    },
    {
      accessorKey: "confirmed_at",
      header: "Confirmed",
      cell: (c) => (c.row.original.confirmed_at ? formatDateTime(c.row.original.confirmed_at) : <span style={{ color: K.muted }}>—</span>),
    },
    {
      id: "actions",
      header: "",
      enableSorting: false,
      cell: (c) => {
        const r = c.row.original;
        if (!canWrite) return null;
        const wait = Math.max(secondsUntil(r.resend_available_at, now), secondsUntil(
          blockedUntil[r.id] ? new Date(blockedUntil[r.id]).toISOString() : null, now));
        return (
          <div style={{ display: "flex", gap: 6, justifyContent: "flex-end", flexWrap: "wrap" }} onClick={(e) => e.stopPropagation()}>
            {r.status === "pending" && (
              <Button size="sm" variant="secondary" disabled={busy !== null || wait > 0} onClick={() => void resend(r)}
                title={wait > 0 ? `Available in ${formatWait(wait)}` : "Send the AWS confirmation e-mail again"}>
                <Send size={12} /> {wait > 0 ? `Resend (${formatWait(wait)})` : "Resend"}
              </Button>
            )}
            <Button size="sm" variant="danger" disabled={busy !== null} onClick={() => setRemoving(r)} ariaLabel={`Remove ${r.email}`}>
              <Trash2 size={12} /> Remove
            </Button>
          </div>
        );
      },
    },
  // eslint-disable-next-line react-hooks/exhaustive-deps
  ], [canWrite, now, blockedUntil, busy]);

  const historyColumns = useMemo<ColumnDef<AlertRecipient, unknown>[]>(() => [
    { accessorKey: "email", header: "E-mail", cell: (c) => <span style={{ fontFamily: mono, fontSize: 12.5 }}>{c.row.original.email}</span> },
    { id: "status", accessorFn: (r) => r.removed_reason ?? "", header: "Why", cell: (c) => <Badge tone={statusTone(c.row.original)} dot={false}>{statusLabel(c.row.original)}</Badge> },
    { id: "removed", accessorFn: (r) => r.removed_at ?? "", header: "When", cell: (c) => (c.row.original.removed_at ? formatDateTime(c.row.original.removed_at) : "—") },
    { id: "by", accessorFn: (r) => r.removed_by ?? "", header: "By", cell: (c) => c.row.original.removed_by ?? "—" },
  ], []);

  const description = (
    <>
      Everyone here gets every platform alert by e-mail: CloudWatch alarms (CIS, Booking, TripPlanner, shared), security
      alerts, cost anomalies, AWS Budgets and CIS spend e-mails.{" "}
      {data && !canWrite && data.configured && (data.viewer?.auth === "legacy"
        ? "Read-only on a password session: sign in with your company e-mail to change the list."
        : "Read-only.")}
    </>
  );

  return (
    <main className="aa-admin-main" style={{ flex: 1, minWidth: 0, overflowY: "auto", padding: "28px 36px 56px" }}>
      <PageHeader
        title={<span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}><BellRing size={18} /> Alert recipients</span>}
        description={description}
        actions={
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            {canWrite && (
              <Button variant="primary" size="sm" onClick={() => setAdding(true)}
                disabled={!data || data.limits.active >= data.limits.max_active || data.limits.adds_left_today <= 0}>
                <MailPlus size={13} /> Add recipient
              </Button>
            )}
            {canWrite && (
              <Button variant="secondary" size="sm" onClick={() => void sendTest()} disabled={busy !== null || !data?.ops_mail_configured}
                title="Queue a test e-mail through the CIS spend alert path (SES) to every confirmed recipient">
                <Send size={13} /> Send test e-mail
              </Button>
            )}
            <Button variant="secondary" size="sm" onClick={() => void refresh()} disabled={refreshing || list.isFetching}
              title="Ask AWS for the current subscription status">
              <RefreshCw size={13} /> {refreshing ? "Checking…" : "Refresh status"}
            </Button>
          </div>
        }
      />

      {list.isError && (
        <ErrorState message={list.error instanceof Error ? list.error.message : "Could not load the alert recipients."} onRetry={() => list.refetch()} />
      )}

      {data && (
        <div data-testid="alert-recipients-ready" style={{ minWidth: 0 }}>
          {!data.configured && (
            <Notice tone="info">
              Alert recipients are not set up on this environment (ALERTS_TOPIC_ARN or ALERT_BOOTSTRAP_EMAIL is not set).
              The list is read-only.
            </Notice>
          )}
          {data.reconcile_error && (
            <Notice tone="warning">
              Could not read the current status from AWS ({data.reconcile_error}). The list below is the last known state.
            </Notice>
          )}
          {pageError && <Notice tone="danger">{pageError}</Notice>}

          <Section
            title={data.topic_name ? `Recipients (topic ${data.topic_name})` : "Recipients"}
            right={<span style={{ fontSize: 12.5, color: K.muted }}>{data.limits.active} of {data.limits.max_active} · {data.limits.adds_left_today} add(s) left today{data.reconciled_at ? ` · checked ${formatDateTime(data.reconciled_at)}` : ""}</span>}
          >
            {data.bootstrap && (
              <LockedRow email={data.bootstrap.email} state={data.bootstrap.state}
                label="Bootstrap: set in Terraform (alert_email); always receives every alert and cannot be removed here." />
            )}
            {data.ops_fallback_email && (
              <LockedRow email={data.ops_fallback_email} label="CIS spend e-mails only: set in Terraform (cis_ops_alert_email)." />
            )}
            <div style={{ marginTop: 10 }}>
              <DataTable<AlertRecipient>
                tableId="platform-alert-recipients"
                data={data.recipients}
                columns={columns}
                getRowId={(r) => r.id}
                paginate={false}
                tableMinWidth={680}
                emptyTitle="No other recipients"
                emptyDescription={canWrite ? "Add a recipient: AWS e-mails them a confirmation link." : "Only the locked recipient above receives alerts."}
              />
            </div>
          </Section>

          {(data.leaving ?? []).length > 0 && (
            <details style={{ marginBottom: 16 }}>
              <summary style={summaryStyle}>Removed, waiting for AWS to drop the unconfirmed subscription ({(data.leaving ?? []).length})</summary>
              <p style={{ fontSize: 12.5, color: K.muted, margin: "8px 0" }}>
                Removed on this page before the confirmation link was clicked. AWS deletes the unconfirmed subscription within
                3 days; if the link is clicked first, the automatic status check (every 5 minutes) unsubscribes it again.
                Nothing to do here.
              </p>
              {(data.leaving ?? []).map((u) => (
                <LockedRow key={u.email} email={u.email} state={u.state} label="Removed here, unconfirmed" />
              ))}
            </details>
          )}

          {data.unmanaged.length > 0 && (
            <details style={{ marginBottom: 16 }}>
              <summary style={summaryStyle}>Unmanaged subscriptions ({data.unmanaged.length})</summary>
              <p style={{ fontSize: 12.5, color: K.muted, margin: "8px 0" }}>
                Added outside this page (AWS console or CLI). They receive alerts too; change them where they were added.
              </p>
              {data.unmanaged.map((u) => (
                <LockedRow key={u.email} email={u.email} state={u.state} label="Added outside the admin page" />
              ))}
            </details>
          )}

          {data.history.length > 0 && (
            <details>
              <summary style={summaryStyle}>History ({data.history.length})</summary>
              <div style={{ marginTop: 8 }}>
                <DataTable<AlertRecipient>
                  tableId="platform-alert-recipients-history"
                  data={data.history}
                  columns={historyColumns}
                  getRowId={(r) => r.id}
                  paginate={false}
                  tableMinWidth={560}
                />
              </div>
            </details>
          )}
        </div>
      )}

      <AddModal
        open={adding}
        onClose={() => setAdding(false)}
        limits={data?.limits}
        active={data?.limits.active ?? 0}
        onAdded={(email, state) => {
          setAdding(false);
          toast.success(state === "confirmed"
            ? `${email} was already confirmed on AWS: it receives alerts now.`
            : `Added ${email}. AWS sent a confirmation e-mail from no-reply@sns.amazonaws.com; alerts start after the link is clicked (check spam).`, 8000);
          void reload();
        }}
      />
      <ConfirmModal
        open={removing !== null}
        onClose={() => setRemoving(null)}
        onConfirm={() => void remove()}
        destructive
        busy={busy !== null}
        title="Remove alert recipient"
        confirmLabel="Remove"
        body={
          <p style={{ margin: 0, fontSize: 13, lineHeight: 1.5, fontFamily: sans }}>
            Remove <strong style={{ fontFamily: mono }}>{removing?.email}</strong>? It stops getting platform alerts and
            CIS spend e-mails. You can add it again later (AWS then sends a new confirmation).
          </p>
        }
      />
    </main>
  );
}

const summaryStyle: React.CSSProperties = {
  cursor: "pointer", fontSize: 13.5, fontWeight: 700, color: K.ink, fontFamily: sans, padding: "6px 0",
};
