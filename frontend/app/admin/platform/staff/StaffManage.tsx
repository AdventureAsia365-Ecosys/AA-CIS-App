"use client";
// app/admin/platform/staff/StaffManage.tsx
// AA-816 C6 — the write half of Platform › People & access › Staff: create a staff account, change its CIS
// role and Booking role, disable / enable it, sign it out everywhere ("reset"), and retry the Cognito half of a
// change that was saved in CIS but not finished in Cognito.
//
// Shown only to a platform owner on a company (Cognito) sign-in: lib/rbac.ts canWriteStaff() reads the `viewer`
// block of GET /api/admin/platform/staff. The backend is the authority (api/routers/admin_platform.py, staff.write
// in api/core/staff_authz.py; api/services/staff_directory.py writes the DB first, then Cognito) and refuses
// every write from a password session or a non-owner.
//
// Outcomes: "ok" (saved everywhere), "pending" (HTTP 502 status saved_cognito_pending: saved in CIS, a Cognito
// step failed; the banner offers "Retry Cognito", POST …/sync), "error" (nothing changed; the backend's reason).
//
// S242 invitation e-mail: Cognito is told not to mail anyone (SUPPRESS: its mail carries a temporary password), so
// CIS sends its own invitation through SES from a background job (api/services/staff_invite.py). "Add staff" has a
// "Send invitation email" checkbox (on by default) and the banner says whether one was queued; the drawer has
// "Resend invite" (POST …/invite: active accounts, at most 1 per 10 minutes and 5 per day, 429 otherwise) and the
// list / drawer show "Last invited" (read from the audit log by the backend).

import { useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, Mail, RefreshCw, XCircle } from "lucide-react";
import { useMemo, useState } from "react";
import { Button, ConfirmModal, K, Modal, RADIUS, sans, useT, Checkbox, Input, Select } from "../../../_kit";
import { ASSIGNABLE_STAFF_ROLES, isStaffRole, staffErrorMessage } from "@/lib/rbac";
import { RBAC_MESSAGES } from "@/lib/rbac.messages";
import type { Translator } from "../../../_kit/core/i18n";
import { BOOKING_ROLE, MANAGE } from "./messages";

// Mirrors the backend row (api/routers/admin_platform.py STAFF_COLUMNS).
export interface StaffAccount {
  id: string;
  username: string;
  role: string;
  is_active: boolean;
  created_at: string | null;
  updated_at: string | null;
  email: string | null;
  display_name: string | null;
  is_owner: boolean;
  last_login_at: string | null;
  linked: boolean;
  booking_role: string | null;
  breakglass: boolean;
  // S242 invitation e-mail (from acp_shared.audit_log): SES accepted / requested / not sent, and the derived state.
  last_invited_at?: string | null;
  last_invite_requested_at?: string | null;
  last_invite_not_sent_at?: string | null;
  invite_status?: InviteStatus | null;
}

export type InviteStatus = "sent" | "queued" | "not_sent";

type InviteFields = Pick<StaffAccount, "invite_status" | "last_invited_at" | "last_invite_requested_at" | "last_invite_not_sent_at">;

export const BOOKING_ROLES = ["admin", "sales", "contracting", "content_creator"] as const;
export type BookingRole = (typeof BOOKING_ROLES)[number];
/** English Booking role names (the `en` side of ./messages.ts BOOKING_ROLE). */
export const BOOKING_ROLE_LABEL: Record<BookingRole, string> = BOOKING_ROLE.en;

/** AA-994: the staff label helpers in the UI language — CIS role, Booking role and "Last invited" in words. */
export function useStaffWords() {
  const m = useT(MANAGE);
  const role = useT(RBAC_MESSAGES);
  const booking = useT(BOOKING_ROLE);
  return useMemo(() => ({
    roleLabel: (r: string) => (isStaffRole(r) ? role(r) : r),
    bookingRoleLabel: (r: string | null) =>
      (!r ? m("none") : Object.hasOwn(BOOKING_ROLE_LABEL, r) ? booking(r as BookingRole) : r),
    /** "Last invited" in words, for the list and the drawer. */
    inviteLabel: (s: InviteFields, fmt: (iso: string) => string) => {
      switch (s.invite_status) {
        case "sent":
          return s.last_invited_at ? fmt(s.last_invited_at) : m("sent");
        case "queued":
          return s.last_invite_requested_at ? m("sendingRequested", fmt(s.last_invite_requested_at)) : m("sending");
        case "not_sent":
          return s.last_invite_not_sent_at ? m("notSentAt", fmt(s.last_invite_not_sent_at)) : m("notSent");
        default:
          return m("never");
      }
    },
  }), [m, role, booking]);
}

/** The refusal words of staffWrite(), in the UI language. */
interface WriteWords {
  checkForm: (msgs: string) => string;
  network: string;
  savedPending: string;
  failed: (status: number) => string;
  adminOnly: string;
}

function useWriteWords(): WriteWords {
  const m = useT(MANAGE);
  const role = useT(RBAC_MESSAGES);
  return useMemo(() => ({
    checkForm: (msgs: string) => m("checkForm", msgs),
    network: m("network"),
    savedPending: m("savedPending"),
    failed: (s: number) => m("failed", s),
    adminOnly: role("adminOnly"),
  }), [m, role]);
}

const STAFF_DOMAIN = "@adventure.asia";
const API = "/api/admin/platform/staff";

export type Outcome =
  | { kind: "ok"; message: string; invite?: string }
  | { kind: "pending"; message: string; staffId: string | null; invite?: string }
  | { kind: "error"; message: string };

type WriteBody = {
  status?: string;
  detail?: unknown;
  staff?: StaffAccount | null;
  failed_step?: string;
  invite?: string;
};

function detailText(detail: unknown, fallback: string, status: number, w: WriteWords): string {
  // FastAPI 422: a list of {loc, msg}; everything else from this API is a string.
  if (Array.isArray(detail)) {
    const msgs = detail.map((d) => (d && typeof d === "object" && "msg" in d ? String(d.msg) : "")).filter(Boolean);
    return msgs.length ? w.checkForm(msgs.join("; ")) : fallback;
  }
  return staffErrorMessage(status, detail, fallback, w.adminOnly);
}

/** One staff directory write through the BFF. Never throws: the result is an Outcome. */
async function staffWrite(
  method: "POST" | "PATCH", path: string, okMessage: string, w: WriteWords, body?: unknown,
): Promise<Outcome> {
  let res: Response;
  try {
    res = await fetch(path, {
      method,
      headers: { "Content-Type": "application/json" },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    return { kind: "error", message: w.network };
  }
  let data: WriteBody = {};
  try {
    data = (await res.json()) as WriteBody;
  } catch {
    /* non-JSON body */
  }
  if (res.ok) return { kind: "ok", message: okMessage, invite: data.invite };
  if (res.status === 502 && data.status === "saved_cognito_pending") {
    const detail = typeof data.detail === "string" ? data.detail : w.savedPending;
    return { kind: "pending", message: detail, staffId: data.staff?.id ?? null, invite: data.invite };
  }
  return { kind: "error", message: detailText(data.detail, w.failed(res.status), res.status, w) };
}

/** The create banner's sentence about the invitation (the backend says what happened in `invite`). */
function createdMessage(email: string, invite: string | undefined, m: Translator<typeof MANAGE.en>): string {
  switch (invite) {
    case "queued":
      return m("createdQueued", email);
    case "not_configured":
      return m("createdNoMail", email);
    case "existing_account":
      return m("existing", email);
    default:
      return m("createdNoInvite", email);
  }
}

function useRefreshStaff() {
  const qc = useQueryClient();
  // ["platform-staff"] is the list; ["platform-staff", id] the drawer: one prefix refreshes both.
  return () => qc.invalidateQueries({ queryKey: ["platform-staff"] });
}

// ── Outcome banner ───────────────────────────────────────────────────────────────────────────────

export function OutcomeBanner({ outcome, onRetried }: { outcome: Outcome; onRetried: (next: Outcome) => void }) {
  const m = useT(MANAGE);
  const w = useWriteWords();
  const refresh = useRefreshStaff();
  const [busy, setBusy] = useState(false);
  const tone = outcome.kind === "ok"
    ? { bg: K.successSoft, fg: K.success, icon: <CheckCircle2 size={16} /> }
    : outcome.kind === "pending"
      ? { bg: K.amberSoft, fg: K.amber, icon: <AlertTriangle size={16} /> }
      : { bg: K.dangerSoft, fg: K.danger, icon: <XCircle size={16} /> };

  const retry = async () => {
    if (outcome.kind !== "pending" || !outcome.staffId) return;
    setBusy(true);
    const next = await staffWrite("POST", `${API}/${encodeURIComponent(outcome.staffId)}/sync`, m("inSync"), w);
    setBusy(false);
    await refresh();
    onRetried(next);
  };

  return (
    <div role="status" aria-live="polite" data-testid={`staff-outcome-${outcome.kind}`} style={{
      display: "flex", gap: 10, alignItems: "flex-start", padding: "10px 12px", borderRadius: RADIUS.md,
      background: tone.bg, color: tone.fg, fontSize: 13, lineHeight: 1.45, fontFamily: sans,
    }}>
      <span style={{ flexShrink: 0, marginTop: 1 }}>{tone.icon}</span>
      <span style={{ flex: 1, minWidth: 0, color: K.ink }}>
        {outcome.kind === "pending" && <strong style={{ color: tone.fg }}>{m("pendingTitle")}</strong>}
        {outcome.message}
      </span>
      {outcome.kind === "pending" && outcome.staffId && (
        <Button size="sm" variant="secondary" onClick={retry} disabled={busy} style={{ flexShrink: 0 }}>
          <RefreshCw size={12} /> {busy ? m("retrying") : m("retryCognito")}
        </Button>
      )}
    </div>
  );
}

// ── Form bits ────────────────────────────────────────────────────────────────────────────────────

const fieldStyle: React.CSSProperties = {
  fontSize: 13.5, padding: "8px 10px", border: `1px solid ${K.line}`, borderRadius: RADIUS.sm, background: K.card,
  color: K.ink, width: "100%", boxSizing: "border-box", fontFamily: sans, minHeight: 36,
};

function Label({ text, htmlFor, hint, children }: { text: string; htmlFor: string; hint?: string; children: React.ReactNode }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 5, minWidth: 0 }}>
      <label htmlFor={htmlFor} style={{ fontSize: 12.5, fontWeight: 600, color: K.ink3 }}>{text}</label>
      {children}
      {hint && <span style={{ fontSize: 12, color: K.muted }}>{hint}</span>}
    </div>
  );
}

function CisRoleSelect({ id, value, onChange, disabled }: { id: string; value: string; onChange: (v: string) => void; disabled?: boolean }) {
  const { roleLabel } = useStaffWords();
  return (
    <Select id={id} value={value} disabled={disabled} onChange={(e) => onChange(e.target.value)} style={fieldStyle}>
      {/* AA-878 — the read-only viewer is never assignable here; a viewer row shows its role, not selectable. */}
      {!(ASSIGNABLE_STAFF_ROLES as readonly string[]).includes(value) && value && (
        <option value={value} disabled>{roleLabel(value)}</option>
      )}
      {ASSIGNABLE_STAFF_ROLES.map((r) => <option key={r} value={r}>{roleLabel(r)}</option>)}
    </Select>
  );
}

function BookingRoleSelect({ id, value, onChange }: { id: string; value: string; onChange: (v: string) => void }) {
  const m = useT(MANAGE);
  const { bookingRoleLabel } = useStaffWords();
  return (
    <Select id={id} value={value} onChange={(e) => onChange(e.target.value)} style={fieldStyle}>
      <option value="">{m("noBooking")}</option>
      {BOOKING_ROLES.map((r) => <option key={r} value={r}>{bookingRoleLabel(r)}</option>)}
    </Select>
  );
}

// ── Create ───────────────────────────────────────────────────────────────────────────────────────

export function CreateStaffModal({ open, onClose, onDone }: { open: boolean; onClose: () => void; onDone: (o: Outcome) => void }) {
  const m = useT(MANAGE);
  const w = useWriteWords();
  const refresh = useRefreshStaff();
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [role, setRole] = useState<string>("content");
  const [booking, setBooking] = useState<string>("");
  const [sendInvite, setSendInvite] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const normalized = email.trim().toLowerCase();
  const domainOk = normalized.endsWith(STAFF_DOMAIN) && normalized.length > STAFF_DOMAIN.length;
  const canSubmit = domainOk && name.trim().length > 0 && !busy;

  const reset = () => {
    setEmail(""); setName(""); setRole("content"); setBooking(""); setSendInvite(true); setError(null);
  };
  const close = () => { if (!busy) { reset(); onClose(); } };

  const submit = async () => {
    if (!canSubmit) return;
    setBusy(true);
    setError(null);
    const outcome = await staffWrite("POST", API, "", w,
      { email: normalized, display_name: name.trim(), role, booking_role: booking || null, send_invite: sendInvite });
    setBusy(false);
    if (outcome.kind === "error") {
      setError(outcome.message);
      return;
    }
    await refresh();
    reset();
    if (outcome.kind === "ok") {
      onDone({ ...outcome, message: createdMessage(normalized, outcome.invite, m) });
    } else {
      onDone(outcome.invite === "queued" ? { ...outcome, message: m("inviteQueuedPending", outcome.message) } : outcome);
    }
  };

  return (
    <Modal open={open} onClose={close} title={m("addTitle")} width={500}
      footer={
        <>
          <Button variant="ghost" onClick={close} disabled={busy}>{m("cancel")}</Button>
          <Button variant="primary" onClick={submit} disabled={!canSubmit}>{busy ? m("creating") : m("createAccount")}</Button>
        </>
      }>
      <form data-testid="staff-create-form" onSubmit={(e) => { e.preventDefault(); void submit(); }}
        style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <Label text={m("workEmail")} htmlFor="staff-create-email"
          hint={email && !domainOk ? m("onlyDomain", STAFF_DOMAIN) : m("emailHint")}>
          <Input id="staff-create-email" type="email" autoComplete="off" value={email} placeholder={`name${STAFF_DOMAIN}`}
            onChange={(e) => setEmail(e.target.value)} style={fieldStyle} aria-invalid={!!email && !domainOk} />
        </Label>
        <Label text={m("displayName")} htmlFor="staff-create-name">
          <Input id="staff-create-name" type="text" autoComplete="off" value={name} maxLength={120}
            onChange={(e) => setName(e.target.value)} style={fieldStyle} />
        </Label>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 14 }}>
          <Label text={m("cisRole")} htmlFor="staff-create-role">
            <CisRoleSelect id="staff-create-role" value={role} onChange={setRole} />
          </Label>
          <Label text={m("bookingRole")} htmlFor="staff-create-booking">
            <BookingRoleSelect id="staff-create-booking" value={booking} onChange={setBooking} />
          </Label>
        </div>
        <label htmlFor="staff-create-invite" style={{ display: "flex", gap: 9, alignItems: "flex-start", cursor: "pointer" }}>
          <Checkbox id="staff-create-invite" checked={sendInvite} data-testid="staff-create-invite"
            onChange={(e) => setSendInvite(e.target.checked)} style={{ marginTop: 3, accentColor: K.accent }} />
          <span style={{ display: "flex", flexDirection: "column", gap: 2 }}>
            <span style={{ fontSize: 13, fontWeight: 600, color: K.ink3 }}>{m("sendInvite")}</span>
            <span style={{ fontSize: 12, color: K.muted, lineHeight: 1.45 }}>{m("sendInviteNote")}</span>
          </span>
        </label>
        {error && <OutcomeBanner outcome={{ kind: "error", message: error }} onRetried={() => {}} />}
        <button type="submit" hidden aria-hidden="true" tabIndex={-1} />
      </form>
    </Modal>
  );
}

// ── Manage one account (drawer) ──────────────────────────────────────────────────────────────────

type Confirm = null | "disable" | "sign-out";

export function StaffManagePanel({ staff, selfId, onOutcome }: {
  staff: StaffAccount;
  selfId: string | null;
  onOutcome: (o: Outcome) => void;
}) {
  const m = useT(MANAGE);
  const w = useWriteWords();
  const { roleLabel, bookingRoleLabel } = useStaffWords();
  const refresh = useRefreshStaff();
  const [role, setRole] = useState(staff.role);
  const [booking, setBooking] = useState(staff.booking_role ?? "");
  const [busy, setBusy] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<Confirm>(null);

  const isSelf = !!selfId && selfId === staff.id;
  const base = `${API}/${encodeURIComponent(staff.id)}`;

  if (staff.breakglass) {
    return (
      <Note>{m("breakGlass")}</Note>
    );
  }

  const run = async (key: string, method: "POST" | "PATCH", path: string, ok: string, body?: unknown) => {
    setBusy(key);
    const outcome = await staffWrite(method, path, ok, w, body);
    setBusy(null);
    setConfirm(null);
    await refresh();
    onOutcome(outcome);
  };

  const roleChanged = role !== staff.role;
  const bookingChanged = booking !== (staff.booking_role ?? "");
  const saveRoles = () => {
    const body: Record<string, string | null> = {};
    if (roleChanged) body.role = role;
    if (bookingChanged) body.booking_role = booking || null;
    void run("roles", "PATCH", `${base}/roles`, m("rolesSaved"), body);
  };

  return (
    <section data-testid="staff-manage" aria-label={m("manageAria")} style={{
      display: "flex", flexDirection: "column", gap: 14, padding: 14, border: `1px solid ${K.line}`,
      borderRadius: RADIUS.lg, background: K.card, fontFamily: sans,
    }}>
      <div style={{ fontSize: 13, fontWeight: 700, color: K.ink }}>{m("manageAccess")}</div>
      {!staff.email && (
        <Note>{m("noEmail")}</Note>
      )}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))", gap: 12 }}>
        <Label text={m("cisRole")} htmlFor={`staff-role-${staff.id}`}
          hint={isSelf ? m("ownRole") : undefined}>
          <CisRoleSelect id={`staff-role-${staff.id}`} value={role} onChange={setRole} disabled={isSelf} />
        </Label>
        <Label text={m("bookingRole")} htmlFor={`staff-booking-${staff.id}`}>
          <BookingRoleSelect id={`staff-booking-${staff.id}`} value={booking} onChange={setBooking} />
        </Label>
      </div>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        <Button size="sm" variant="primary" onClick={saveRoles} disabled={(!roleChanged && !bookingChanged) || !!busy}>
          {busy === "roles" ? m("saving") : m("saveRoles")}
        </Button>
        {staff.is_active ? (
          <Button size="sm" variant="danger" onClick={() => setConfirm("disable")} disabled={isSelf || !!busy}
            title={isSelf ? m("selfDisable") : m("disableTitle")}>
            {m("disable")}
          </Button>
        ) : (
          <Button size="sm" variant="secondary" disabled={!!busy}
            onClick={() => void run("enable", "POST", `${base}/enable`, m("enabled"))}>
            {busy === "enable" ? m("enabling") : m("enable")}
          </Button>
        )}
        <Button size="sm" variant="ghost" onClick={() => setConfirm("sign-out")} disabled={!!busy || !staff.email}
          title={m("signOutTitle")}>
          {m("signOutAll")}
        </Button>
        {staff.email && staff.is_active && (
          <Button size="sm" variant="ghost" disabled={!!busy} data-testid="staff-resend-invite"
            title={m("inviteTitle")}
            onClick={() => void run("invite", "POST", `${base}/invite`, m("inviteQueued", staff.email ?? ""))}>
            <Mail size={12} /> {busy === "invite" ? m("sending") : staff.invite_status ? m("resendInvite") : m("sendInviteBtn")}
          </Button>
        )}
        {staff.email && (
          <Button size="sm" variant="ghost" disabled={!!busy}
            title={m("syncTitle")}
            onClick={() => void run("sync", "POST", `${base}/sync`, m("inSync"))}>
            <RefreshCw size={12} /> {busy === "sync" ? m("syncing") : m("retrySync")}
          </Button>
        )}
      </div>
      <div style={{ fontSize: 12, color: K.muted, lineHeight: 1.5 }}>
        {m("eachChange")}
        {roleChanged && role !== staff.role && m("cisChange", roleLabel(staff.role), roleLabel(role))}
        {bookingChanged && m("bookingChange", bookingRoleLabel(staff.booking_role), bookingRoleLabel(booking || null))}
      </div>

      <ConfirmModal
        open={confirm === "disable"}
        onClose={() => setConfirm(null)}
        destructive
        busy={busy === "disable"}
        title={m("disableQ")}
        confirmLabel={m("disable")}
        body={<>
          <strong>{staff.display_name || staff.username}</strong> {m("disableBody")}
        </>}
        onConfirm={() => void run("disable", "POST", `${base}/disable`, m("disabledDone"))}
      />
      <ConfirmModal
        open={confirm === "sign-out"}
        onClose={() => setConfirm(null)}
        busy={busy === "sign-out"}
        title={m("signOutQ")}
        confirmLabel={m("signOutAll")}
        body={<>
          {m("endsEvery")} <strong>{staff.display_name || staff.username}</strong> {m("onEveryDevice", staff.email ?? "")}
          {!staff.is_active && m("alsoEnables")}
          {isSelf && m("ownAccount")}
        </>}
        onConfirm={() => void run("sign-out", "POST", `${base}/sign-out`, staff.is_active
          ? m("signedOut")
          : m("signedOutEnabled"))}
      />
    </section>
  );
}

function Note({ children }: { children: React.ReactNode }) {
  return (
    <div style={{ fontSize: 12.5, color: K.muted, lineHeight: 1.5, padding: "8px 10px", background: K.line2,
      borderRadius: RADIUS.sm }}>
      {children}
    </div>
  );
}
