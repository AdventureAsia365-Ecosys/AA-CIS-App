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
import { useState } from "react";
import { Button, ConfirmModal, K, Modal, RADIUS, sans } from "../../../_kit";
import { ASSIGNABLE_STAFF_ROLES, ROLE_LABEL, isStaffRole, staffErrorMessage } from "@/lib/rbac";

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

/** "Last invited" in words, for the list and the drawer. */
export function inviteLabel(s: Pick<StaffAccount, "invite_status" | "last_invited_at" | "last_invite_requested_at" | "last_invite_not_sent_at">,
  fmt: (iso: string) => string): string {
  switch (s.invite_status) {
    case "sent":
      return s.last_invited_at ? fmt(s.last_invited_at) : "Sent";
    case "queued":
      return s.last_invite_requested_at ? `Sending (requested ${fmt(s.last_invite_requested_at)})` : "Sending";
    case "not_sent":
      return s.last_invite_not_sent_at
        ? `Not sent (${fmt(s.last_invite_not_sent_at)}); see Operations › Jobs`
        : "Not sent; see Operations › Jobs";
    default:
      return "Never";
  }
}

export const BOOKING_ROLES = ["admin", "sales", "contracting", "content_creator"] as const;
export type BookingRole = (typeof BOOKING_ROLES)[number];
export const BOOKING_ROLE_LABEL: Record<BookingRole, string> = {
  admin: "Booking admin",
  sales: "Sales",
  contracting: "Contracting",
  content_creator: "Content creator",
};

export function bookingRoleLabel(role: string | null): string {
  if (!role) return "None";
  return (BOOKING_ROLE_LABEL as Record<string, string>)[role] ?? role;
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

function detailText(detail: unknown, fallback: string, status: number): string {
  // FastAPI 422: a list of {loc, msg}; everything else from this API is a string.
  if (Array.isArray(detail)) {
    const msgs = detail.map((d) => (d && typeof d === "object" && "msg" in d ? String(d.msg) : "")).filter(Boolean);
    return msgs.length ? `Check the form: ${msgs.join("; ")}` : fallback;
  }
  return staffErrorMessage(status, detail, fallback);
}

/** One staff directory write through the BFF. Never throws: the result is an Outcome. */
async function staffWrite(method: "POST" | "PATCH", path: string, okMessage: string, body?: unknown): Promise<Outcome> {
  let res: Response;
  try {
    res = await fetch(path, {
      method,
      headers: { "Content-Type": "application/json" },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    return { kind: "error", message: "Network error: nothing was sent. Check your connection and try again." };
  }
  let data: WriteBody = {};
  try {
    data = (await res.json()) as WriteBody;
  } catch {
    /* non-JSON body */
  }
  if (res.ok) return { kind: "ok", message: okMessage, invite: data.invite };
  if (res.status === 502 && data.status === "saved_cognito_pending") {
    const detail = typeof data.detail === "string" ? data.detail : "Saved in CIS; the company sign-in was not updated.";
    return { kind: "pending", message: detail, staffId: data.staff?.id ?? null, invite: data.invite };
  }
  return { kind: "error", message: detailText(data.detail, `Request failed (${res.status})`, res.status) };
}

/** The create banner's sentence about the invitation (the backend says what happened in `invite`). */
function createdMessage(email: string, invite: string | undefined): string {
  switch (invite) {
    case "queued":
      return `Created ${email}. An invitation email with the sign-in steps goes to that address in about 2 minutes.`;
    case "not_configured":
      return `Created ${email}. Invitation email is not set up on this server, so none was sent: tell them to sign in with "Sign in with company email" and this address.`;
    case "existing_account":
      return `${email} already had this account; no new invitation was sent (use "Resend invite" on their row).`;
    default:
      return `Created ${email}. No invitation was sent: tell them to sign in with "Sign in with company email" and this address, or send one later from their row.`;
  }
}

function useRefreshStaff() {
  const qc = useQueryClient();
  // ["platform-staff"] is the list; ["platform-staff", id] the drawer: one prefix refreshes both.
  return () => qc.invalidateQueries({ queryKey: ["platform-staff"] });
}

function roleLabel(role: string): string {
  return isStaffRole(role) ? ROLE_LABEL[role] : role;
}

// ── Outcome banner ───────────────────────────────────────────────────────────────────────────────

export function OutcomeBanner({ outcome, onRetried }: { outcome: Outcome; onRetried: (next: Outcome) => void }) {
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
    const next = await staffWrite("POST", `${API}/${encodeURIComponent(outcome.staffId)}/sync`,
      "Company sign-in is now in sync with CIS.");
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
        {outcome.kind === "pending" && <strong style={{ color: tone.fg }}>Saved, sign-in update pending. </strong>}
        {outcome.message}
      </span>
      {outcome.kind === "pending" && outcome.staffId && (
        <Button size="sm" variant="secondary" onClick={retry} disabled={busy} style={{ flexShrink: 0 }}>
          <RefreshCw size={12} /> {busy ? "Retrying…" : "Retry Cognito"}
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
  return (
    <select id={id} value={value} disabled={disabled} onChange={(e) => onChange(e.target.value)} style={fieldStyle}>
      {/* AA-878 — the read-only viewer is never assignable here; a viewer row shows its role, not selectable. */}
      {!(ASSIGNABLE_STAFF_ROLES as readonly string[]).includes(value) && value && (
        <option value={value} disabled>{roleLabel(value)}</option>
      )}
      {ASSIGNABLE_STAFF_ROLES.map((r) => <option key={r} value={r}>{ROLE_LABEL[r]}</option>)}
    </select>
  );
}

function BookingRoleSelect({ id, value, onChange }: { id: string; value: string; onChange: (v: string) => void }) {
  return (
    <select id={id} value={value} onChange={(e) => onChange(e.target.value)} style={fieldStyle}>
      <option value="">No Booking access</option>
      {BOOKING_ROLES.map((r) => <option key={r} value={r}>{BOOKING_ROLE_LABEL[r]}</option>)}
    </select>
  );
}

// ── Create ───────────────────────────────────────────────────────────────────────────────────────

export function CreateStaffModal({ open, onClose, onDone }: { open: boolean; onClose: () => void; onDone: (o: Outcome) => void }) {
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
    const outcome = await staffWrite("POST", API, "",
      { email: normalized, display_name: name.trim(), role, booking_role: booking || null, send_invite: sendInvite });
    setBusy(false);
    if (outcome.kind === "error") {
      setError(outcome.message);
      return;
    }
    await refresh();
    reset();
    if (outcome.kind === "ok") {
      onDone({ ...outcome, message: createdMessage(normalized, outcome.invite) });
    } else {
      onDone(outcome.invite === "queued"
        ? { ...outcome, message: `${outcome.message} An invitation email is queued and waits until the retry succeeds; if that takes longer than about 10 minutes it is dropped, so use "Resend invite" afterwards.` }
        : outcome);
    }
  };

  return (
    <Modal open={open} onClose={close} title="Add staff account" width={500}
      footer={
        <>
          <Button variant="ghost" onClick={close} disabled={busy}>Cancel</Button>
          <Button variant="primary" onClick={submit} disabled={!canSubmit}>{busy ? "Creating…" : "Create account"}</Button>
        </>
      }>
      <form data-testid="staff-create-form" onSubmit={(e) => { e.preventDefault(); void submit(); }}
        style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <Label text="Work email" htmlFor="staff-create-email"
          hint={email && !domainOk ? `Only ${STAFF_DOMAIN} addresses can be staff accounts.` : "The person signs in with this address; no password is set."}>
          <input id="staff-create-email" type="email" autoComplete="off" value={email} placeholder={`name${STAFF_DOMAIN}`}
            onChange={(e) => setEmail(e.target.value)} style={fieldStyle} aria-invalid={!!email && !domainOk} />
        </Label>
        <Label text="Display name" htmlFor="staff-create-name">
          <input id="staff-create-name" type="text" autoComplete="off" value={name} maxLength={120}
            onChange={(e) => setName(e.target.value)} style={fieldStyle} />
        </Label>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 14 }}>
          <Label text="CIS role" htmlFor="staff-create-role">
            <CisRoleSelect id="staff-create-role" value={role} onChange={setRole} />
          </Label>
          <Label text="Booking role" htmlFor="staff-create-booking">
            <BookingRoleSelect id="staff-create-booking" value={booking} onChange={setBooking} />
          </Label>
        </div>
        <label htmlFor="staff-create-invite" style={{ display: "flex", gap: 9, alignItems: "flex-start", cursor: "pointer" }}>
          <input id="staff-create-invite" type="checkbox" checked={sendInvite} data-testid="staff-create-invite"
            onChange={(e) => setSendInvite(e.target.checked)} style={{ marginTop: 3, accentColor: K.accent }} />
          <span style={{ display: "flex", flexDirection: "column", gap: 2 }}>
            <span style={{ fontSize: 13, fontWeight: 600, color: K.ink3 }}>Send invitation email</span>
            <span style={{ fontSize: 12, color: K.muted, lineHeight: 1.45 }}>
              Tells them they were added, their roles, and how to sign in with a one-time code. No password is sent.
            </span>
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
  const refresh = useRefreshStaff();
  const [role, setRole] = useState(staff.role);
  const [booking, setBooking] = useState(staff.booking_role ?? "");
  const [busy, setBusy] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<Confirm>(null);

  const isSelf = !!selfId && selfId === staff.id;
  const base = `${API}/${encodeURIComponent(staff.id)}`;

  if (staff.breakglass) {
    return (
      <Note>
        Break-glass account: it is enabled and disabled only through its runbook, never from this page.
      </Note>
    );
  }

  const run = async (key: string, method: "POST" | "PATCH", path: string, ok: string, body?: unknown) => {
    setBusy(key);
    const outcome = await staffWrite(method, path, ok, body);
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
    void run("roles", "PATCH", `${base}/roles`, "Roles saved in CIS and the company sign-in.", body);
  };

  return (
    <section data-testid="staff-manage" aria-label="Manage this account" style={{
      display: "flex", flexDirection: "column", gap: 14, padding: 14, border: `1px solid ${K.line}`,
      borderRadius: RADIUS.lg, background: K.card, fontFamily: sans,
    }}>
      <div style={{ fontSize: 13, fontWeight: 700, color: K.ink }}>Manage access</div>
      {!staff.email && (
        <Note>No work email on this account: changes apply to CIS only (it has no company sign-in yet).</Note>
      )}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))", gap: 12 }}>
        <Label text="CIS role" htmlFor={`staff-role-${staff.id}`}
          hint={isSelf ? "You cannot change your own CIS role." : undefined}>
          <CisRoleSelect id={`staff-role-${staff.id}`} value={role} onChange={setRole} disabled={isSelf} />
        </Label>
        <Label text="Booking role" htmlFor={`staff-booking-${staff.id}`}>
          <BookingRoleSelect id={`staff-booking-${staff.id}`} value={booking} onChange={setBooking} />
        </Label>
      </div>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        <Button size="sm" variant="primary" onClick={saveRoles} disabled={(!roleChanged && !bookingChanged) || !!busy}>
          {busy === "roles" ? "Saving…" : "Save roles"}
        </Button>
        {staff.is_active ? (
          <Button size="sm" variant="danger" onClick={() => setConfirm("disable")} disabled={isSelf || !!busy}
            title={isSelf ? "You cannot disable yourself" : "Block sign-in and end every session"}>
            Disable
          </Button>
        ) : (
          <Button size="sm" variant="secondary" disabled={!!busy}
            onClick={() => void run("enable", "POST", `${base}/enable`, "Enabled: the account can sign in again.")}>
            {busy === "enable" ? "Enabling…" : "Enable"}
          </Button>
        )}
        <Button size="sm" variant="ghost" onClick={() => setConfirm("sign-out")} disabled={!!busy || !staff.email}
          title="End every session of this account; re-enables it if it is disabled">
          Sign out everywhere
        </Button>
        {staff.email && staff.is_active && (
          <Button size="sm" variant="ghost" disabled={!!busy} data-testid="staff-resend-invite"
            title="Email the sign-in invitation to this address (at most once every 10 minutes, 5 a day)"
            onClick={() => void run("invite", "POST", `${base}/invite`,
              `Invitation queued: an email with the sign-in steps is on its way to ${staff.email}.`)}>
            <Mail size={12} /> {busy === "invite" ? "Sending…" : staff.invite_status ? "Resend invite" : "Send invite"}
          </Button>
        )}
        {staff.email && (
          <Button size="sm" variant="ghost" disabled={!!busy}
            title="Make the company sign-in match this row again (after a pending change)"
            onClick={() => void run("sync", "POST", `${base}/sync`, "Company sign-in is now in sync with CIS.")}>
            <RefreshCw size={12} /> {busy === "sync" ? "Syncing…" : "Retry Cognito sync"}
          </Button>
        )}
      </div>
      <div style={{ fontSize: 12, color: K.muted, lineHeight: 1.5 }}>
        Each change is saved in CIS first, then in the company sign-in (Cognito), and recorded in the audit log.
        {roleChanged && role !== staff.role && ` CIS role: ${roleLabel(staff.role)} → ${roleLabel(role)}.`}
        {bookingChanged && ` Booking role: ${bookingRoleLabel(staff.booking_role)} → ${bookingRoleLabel(booking || null)}.`}
      </div>

      <ConfirmModal
        open={confirm === "disable"}
        onClose={() => setConfirm(null)}
        destructive
        busy={busy === "disable"}
        title="Disable this account?"
        confirmLabel="Disable"
        body={<>
          <strong>{staff.display_name || staff.username}</strong> can no longer sign in, and every session is ended.
          An access token already issued stays valid for up to 15 minutes. You can enable the account again later.
        </>}
        onConfirm={() => void run("disable", "POST", `${base}/disable`,
          "Disabled: sign-in is blocked and every session was ended (a live token expires within 15 minutes).")}
      />
      <ConfirmModal
        open={confirm === "sign-out"}
        onClose={() => setConfirm(null)}
        busy={busy === "sign-out"}
        title="Sign out everywhere?"
        confirmLabel="Sign out everywhere"
        body={<>
          Ends every session of <strong>{staff.display_name || staff.username}</strong> on every device; they sign in
          again with a code sent to {staff.email}.
          {!staff.is_active && " This account is disabled: this also enables it again."}
          {isSelf && " This is your own account: you will be signed out too."}
        </>}
        onConfirm={() => void run("sign-out", "POST", `${base}/sign-out`, staff.is_active
          ? "Signed out everywhere."
          : "Signed out everywhere and enabled again.")}
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
