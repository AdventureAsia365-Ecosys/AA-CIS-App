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
  mono,
  sans,
  useFormat,
  useLang,
  useT,
  useToast,
  type ColumnDef, AdminMain,
} from "../../../_kit";
import { staffErrorMessage } from "@/lib/rbac";
import { RBAC_MESSAGES } from "@/lib/rbac.messages";
import {
  emailLooksValid,
  formatWait,
  isCompanyDomain,
  companyDomainSuggestion,
  retryAfterSeconds,
  secondsUntil,
  snsStateLabel,
  statusLabel,
  statusTone,
  type AlertRecipient,
  type AlertRecipientsPage,
  type SnsState,
} from "@/lib/alertRecipients";
import { ALERT_WORDS_MESSAGES } from "@/lib/alertRecipients.messages";
import { ALERTS } from "./messages";

const API = "/api/admin/platform/alert-recipients";
const KEY = ["platform-alert-recipients"] as const;

type WriteResult = { ok: true; data: Record<string, unknown> } | { ok: false; message: string; retryAfter?: number };

/** The refusal words of send(), in the UI language. */
interface SendWords {
  network: string;
  formRefused: string;
  failed: (status: number) => string;
  adminOnly: string;
}

function useSendWords(): SendWords {
  const t = useT(ALERTS);
  const r = useT(RBAC_MESSAGES);
  return useMemo(() => ({
    network: t("network"), formRefused: t("formRefused"), failed: (s: number) => t("failed", s), adminOnly: r("adminOnly"),
  }), [t, r]);
}

/** One change through the BFF. Never throws. */
async function send(method: "POST" | "DELETE", path: string, w: SendWords, body?: unknown): Promise<WriteResult> {
  let res: Response;
  try {
    res = await fetch(path, {
      method,
      headers: { "Content-Type": "application/json" },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    return { ok: false, message: w.network };
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
    message = w.formRefused;
  } else {
    message = staffErrorMessage(res.status, data.detail, w.failed(res.status), w.adminOnly);
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
  const t = useT(ALERTS);
  const lang = useLang();
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", padding: "8px 0", borderTop: `1px solid ${K.line}` }}>
      <Lock size={14} style={{ color: K.muted, flex: "none" }} aria-label={t("locked")} />
      <span style={{ fontFamily: mono, fontSize: 13, color: K.ink, overflowWrap: "anywhere" }}>{email}</span>
      {state !== undefined && (
        <Badge tone={state === "confirmed" ? "success" : state === "pending" ? "warning" : "danger"} dot={false}>
          {snsStateLabel(state, ALERT_WORDS_MESSAGES[lang])}
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
  const t = useT(ALERTS);
  const sw = useSendWords();
  const [email, setEmail] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const maxNote = limits?.max_note ?? 80;
  const trimmed = email.trim();
  const valid = emailLooksValid(trimmed);
  const outside = valid && !isCompanyDomain(trimmed);
  const suggestion = valid ? companyDomainSuggestion(trimmed) : null;
  const [keepTypo, setKeepTypo] = useState(false);
  const blockedByTypo = !!suggestion && !keepTypo;
  const noteTooLong = note.trim().length > maxNote;

  const close = () => {
    if (busy) return;
    setEmail("");
    setKeepTypo(false);
    setNote("");
    setError(null);
    onClose();
  };

  const submit = async () => {
    if (!valid || noteTooLong || busy || blockedByTypo) return;
    setBusy(true);
    setError(null);
    const out = await send("POST", API, sw, { email: trimmed, note: note.trim() || undefined });
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
      title={t("addTitle")}
      width={480}
      footer={
        <>
          <Button variant="ghost" onClick={close} disabled={busy}>{t("cancel")}</Button>
          <Button variant="primary" onClick={submit} disabled={!valid || noteTooLong || busy || blockedByTypo}>
            <MailPlus size={13} /> {busy ? t("adding") : t("addAndSend")}
          </Button>
        </>
      }
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 12, fontFamily: sans }}>
        <p style={{ margin: 0, fontSize: 13, color: K.body, lineHeight: 1.5 }}>
          {t("awsSendsFrom")} <span style={{ fontFamily: mono }}>no-reply@sns.amazonaws.com</span>{t("awsSubject")}{" "}
          {limits ? t("used", active, limits.max_active) : ""}
        </p>
        <label style={{ display: "flex", flexDirection: "column", gap: 5, fontSize: 12.5, fontWeight: 600, color: K.ink3 }}>
          {t("email")}
          <input
            type="email"
            autoComplete="off"
            value={email}
            onChange={(e) => { setEmail(e.target.value); setKeepTypo(false); }}
            onKeyDown={(e) => { if (e.key === "Enter") void submit(); }}
            placeholder="name@adventure.asia"
            aria-invalid={trimmed.length > 0 && !valid}
            style={inputStyle}
          />
        </label>
        {trimmed.length > 0 && !valid && (
          <div style={{ fontSize: 12.5, color: K.danger }}>
            {t("badEmail")}
          </div>
        )}
        {suggestion && (
          <Notice tone="danger">
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              <span>
                {t("didYouMean")} <strong style={{ fontFamily: mono }}>{suggestion}</strong>{t("typoNote")}
              </span>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
                <Button variant="secondary" onClick={() => { setEmail(suggestion); setKeepTypo(false); }}>
                  {t("use", suggestion)}
                </Button>
                <label style={{ display: "flex", gap: 6, alignItems: "center", fontSize: 12.5 }}>
                  <input id="alert-keep-typo" type="checkbox" checked={keepTypo} onChange={(e) => setKeepTypo(e.target.checked)} />
                  {t("keepTyped")}
                </label>
              </div>
            </div>
          </Notice>
        )}
        {outside && !suggestion && (
          <Notice tone="warning">
            {t("outside", "adventure.asia")}
          </Notice>
        )}
        <label style={{ display: "flex", flexDirection: "column", gap: 5, fontSize: 12.5, fontWeight: 600, color: K.ink3 }}>
          {t("note")}
          <input
            type="text"
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder={t("notePlaceholder")}
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
  const t = useT(ALERTS);
  const lang = useLang();
  const aw = ALERT_WORDS_MESSAGES[lang];
  const sw = useSendWords();
  const { formatDateTime } = useFormat();
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
    const out = await send("POST", `${API}/refresh`, sw);
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
    const out = await send("POST", `${API}/${encodeURIComponent(r.id)}/resend`, sw);
    setBusy(null);
    if (!out.ok) {
      if (out.retryAfter) setBlockedUntil((prev) => ({ ...prev, [r.id]: Date.now() + out.retryAfter! * 1000 }));
      setPageError(out.message);
      return;
    }
    toast.success(t("resentToast", r.email), 6000);
    void reload();
  };

  const remove = async () => {
    const r = removing;
    if (!r) return;
    setBusy(r.id);
    setPageError(null);
    const out = await send("DELETE", `${API}/${encodeURIComponent(r.id)}`, sw);
    setBusy(null);
    setRemoving(null);
    if (!out.ok) {
      setPageError(out.message);
      return;
    }
    toast.success(out.data.sns === "pending_left_to_expire"
      ? t("removedPending", r.email)
      : t("removed", r.email), 7000);
    void reload();
  };

  const sendTest = async () => {
    setBusy("test");
    setPageError(null);
    const out = await send("POST", `${API}/test-email`, sw);
    setBusy(null);
    if (!out.ok) {
      setPageError(out.message);
      return;
    }
    toast.success(t("testQueued", String(out.data.recipients_count ?? "")), 6000);
  };

  const columns = useMemo<ColumnDef<AlertRecipient, unknown>[]>(() => [
    {
      accessorKey: "email",
      header: t("colEmail"),
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
      header: t("colStatus"),
      cell: (c) => {
        const r = c.row.original;
        return (
          <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
            <Badge tone={statusTone(r)} dot={false}>{statusLabel(r, aw)}</Badge>
            {r.status === "pending" && (
              <span style={{ fontSize: 11.5, color: K.muted, maxWidth: 240 }}>
                {t("pendingNote")}
              </span>
            )}
          </div>
        );
      },
    },
    {
      id: "added",
      accessorFn: (r) => r.created_at ?? "",
      header: t("colAdded"),
      cell: (c) => (
        <div style={{ fontSize: 12.5 }}>
          <div>{c.row.original.created_at ? formatDateTime(c.row.original.created_at) : "—"}</div>
          <div style={{ color: K.muted }}>{c.row.original.created_by ?? ""}</div>
        </div>
      ),
    },
    {
      accessorKey: "confirmed_at",
      header: t("colConfirmed"),
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
                title={wait > 0 ? t("availableIn", formatWait(wait, aw)) : t("resendTitle")}>
                <Send size={12} /> {wait > 0 ? t("resendIn", formatWait(wait, aw)) : t("resend")}
              </Button>
            )}
            <Button size="sm" variant="danger" disabled={busy !== null} onClick={() => setRemoving(r)} ariaLabel={t("removeAria", r.email)}>
              <Trash2 size={12} /> {t("remove")}
            </Button>
          </div>
        );
      },
    },
  // eslint-disable-next-line react-hooks/exhaustive-deps
  ], [canWrite, now, blockedUntil, busy, t, aw, formatDateTime]);

  const historyColumns = useMemo<ColumnDef<AlertRecipient, unknown>[]>(() => [
    { accessorKey: "email", header: t("colEmail"), cell: (c) => <span style={{ fontFamily: mono, fontSize: 12.5 }}>{c.row.original.email}</span> },
    { id: "status", accessorFn: (r) => r.removed_reason ?? "", header: t("colWhy"), cell: (c) => <Badge tone={statusTone(c.row.original)} dot={false}>{statusLabel(c.row.original, aw)}</Badge> },
    { id: "removed", accessorFn: (r) => r.removed_at ?? "", header: t("colWhen"), cell: (c) => (c.row.original.removed_at ? formatDateTime(c.row.original.removed_at) : "—") },
    { id: "by", accessorFn: (r) => r.removed_by ?? "", header: t("colBy"), cell: (c) => c.row.original.removed_by ?? "—" },
  ], [t, aw, formatDateTime]);

  const description = (
    <>
      {t("intro")}{" "}
      {data && !canWrite && data.configured && (data.viewer?.auth === "legacy" ? t("readOnlyLegacy") : t("readOnly"))}
    </>
  );

  return (
    <AdminMain>
      <PageHeader
        title={<span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}><BellRing size={18} /> {t("title")}</span>}
        description={description}
        actions={
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            {canWrite && (
              <Button variant="primary" size="sm" onClick={() => setAdding(true)}
                disabled={!data || data.limits.active >= data.limits.max_active || data.limits.adds_left_today <= 0}>
                <MailPlus size={13} /> {t("addRecipient")}
              </Button>
            )}
            {canWrite && (
              <Button variant="secondary" size="sm" onClick={() => void sendTest()} disabled={busy !== null || !data?.ops_mail_configured}
                title={t("testTitle")}>
                <Send size={13} /> {t("sendTest")}
              </Button>
            )}
            <Button variant="secondary" size="sm" onClick={() => void refresh()} disabled={refreshing || list.isFetching}
              title={t("refreshTitle")}>
              <RefreshCw size={13} /> {refreshing ? t("checking") : t("refreshStatus")}
            </Button>
          </div>
        }
      />

      {list.isError && (
        <ErrorState message={list.error instanceof Error ? list.error.message : t("loadError")} onRetry={() => list.refetch()} />
      )}

      {data && (
        <div data-testid="alert-recipients-ready" style={{ minWidth: 0 }}>
          {!data.configured && (
            <Notice tone="info">
              {t("notSetUp")}
            </Notice>
          )}
          {data.reconcile_error && (
            <Notice tone="warning">
              {t("reconcileError", data.reconcile_error)}
            </Notice>
          )}
          {pageError && <Notice tone="danger">{pageError}</Notice>}

          <Section
            title={data.topic_name ? t("recipientsTopic", data.topic_name) : t("recipients")}
            right={<span style={{ fontSize: 12.5, color: K.muted }}>{t("counts", data.limits.active, data.limits.max_active, data.limits.adds_left_today)}{data.reconciled_at ? t("checkedAt", formatDateTime(data.reconciled_at)) : ""}</span>}
          >
            {data.bootstrap && (
              <LockedRow email={data.bootstrap.email} state={data.bootstrap.state}
                label={t("bootstrap")} />
            )}
            {data.ops_fallback_email && (
              <LockedRow email={data.ops_fallback_email} label={t("opsFallback")} />
            )}
            <div style={{ marginTop: 10 }}>
              <DataTable<AlertRecipient>
                tableId="platform-alert-recipients"
                data={data.recipients}
                columns={columns}
                getRowId={(r) => r.id}
                paginate={false}
                tableMinWidth={680}
                emptyTitle={t("noOthers")}
                emptyDescription={canWrite ? t("emptyWrite") : t("emptyRead")}
              />
            </div>
          </Section>

          {(data.leaving ?? []).length > 0 && (
            <details style={{ marginBottom: 16 }}>
              <summary style={summaryStyle}>{t("leavingTitle", (data.leaving ?? []).length)}</summary>
              <p style={{ fontSize: 12.5, color: K.muted, margin: "8px 0" }}>
                {t("leavingNote")}
              </p>
              {(data.leaving ?? []).map((u) => (
                <LockedRow key={u.email} email={u.email} state={u.state} label={t("leavingLabel")} />
              ))}
            </details>
          )}

          {data.unmanaged.length > 0 && (
            <details style={{ marginBottom: 16 }}>
              <summary style={summaryStyle}>{t("unmanagedTitle", data.unmanaged.length)}</summary>
              <p style={{ fontSize: 12.5, color: K.muted, margin: "8px 0" }}>
                {t("unmanagedNote")}
              </p>
              {data.unmanaged.map((u) => (
                <LockedRow key={u.email} email={u.email} state={u.state} label={t("unmanagedLabel")} />
              ))}
            </details>
          )}

          {data.history.length > 0 && (
            <details>
              <summary style={summaryStyle}>{t("history", data.history.length)}</summary>
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
            ? t("alreadyConfirmed", email)
            : t("added", email), 8000);
          void reload();
        }}
      />
      <ConfirmModal
        open={removing !== null}
        onClose={() => setRemoving(null)}
        onConfirm={() => void remove()}
        destructive
        busy={busy !== null}
        title={t("removeTitle")}
        confirmLabel={t("remove")}
        body={
          <p style={{ margin: 0, fontSize: 13, lineHeight: 1.5, fontFamily: sans }}>
            {t("removeQ")} <strong style={{ fontFamily: mono }}>{removing?.email}</strong>{t("removeBody")}
          </p>
        }
      />
    </AdminMain>
  );
}

const summaryStyle: React.CSSProperties = {
  cursor: "pointer", fontSize: 13.5, fontWeight: 700, color: K.ink, fontFamily: sans, padding: "6px 0",
};
