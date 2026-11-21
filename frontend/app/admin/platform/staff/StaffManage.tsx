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
//
// AA-1031: CRM (AA-Partner) staff roles, several per person (Agency admin, Operations, Support = WORKFORCE groups
// crm-platform-admin / crm-ops / crm-support), set in Add staff and Manage access next to the Booking role. Until the
// database update for CRM roles is applied the list says crm_roles_available: false and the CRM field is read-only.
//
// AA-1150 (lean staff roles step 1): the people-role presets come first (PATCH …/roles {"preset": code}; in Add staff
// the preset fills the role fields); the CIS / Booking / CRM fields sit behind "Show all roles". Booking
// content_creator is retired: never offered, an old grant shows as "(retired)" and Sync removes it.
//
// AA-1155 (spec task T14, R5.7): the presets are data (GET /api/admin/platform/presets, edited by an owner in the
// "People-role presets" section of this page); the Pricing team preset is one row among them, no longer a separate
// block. AA-1155 (spec task T9, R5.3 / R3.3): the CIS and Booking role pickers list each app's custom roles next to the
// built-in ones (GET /api/admin/platform/roles, the same catalogue as Platform › Roles & permissions); saving a custom
// role puts the person in cis-custom / booking-custom (the backend's Sync does the same). The business-owner marker
// (at most one person) is set or cleared here by an owner (PUT …/business-owner), audited by the backend.

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, Mail, RefreshCw, XCircle } from "lucide-react";
import { useMemo, useState } from "react";
import { Button, ConfirmModal, Field, K, Modal, RADIUS, apiGet, sans, useT, Checkbox, Input, Select } from "../../../_kit";
import {
  ROLES_PATH, cisRoleOf, customRoleLabel, customRoleOptions, type RolesCatalogue,
} from "@/lib/rolesMatrix";
import { ASSIGNABLE_STAFF_ROLES, isStaffRole, staffErrorMessage } from "@/lib/rbac";
import { RBAC_MESSAGES } from "@/lib/rbac.messages";
import type { Translator } from "../../../_kit/core/i18n";
import {
  BOOKING_ROLES, CRM_ROLES, RETIRED_BOOKING_ROLES, createRoles, crmRolesOf, type CrmRole, type StaffPreset,
} from "@/lib/staffPresets";
import { BOOKING_ROLE, CRM_ROLE, MANAGE } from "./messages";
import { BusinessOwnerControl } from "./BusinessOwner";
import { PeoplePresets, PresetConfirm, useActivePresets } from "./PeoplePresets";

// AA-1150: the roles and presets live in lib/staffPresets.ts (unit-tested there); re-exported for the directory page.
export { BOOKING_ROLES, CRM_ROLES, crmRolesOf };
export type { BookingRole, CrmRole } from "@/lib/staffPresets";

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
  /** AA-1031: CRM staff role codes (CRM_ROLES order); [] = none. Optional: older API answers lack it. */
  crm_roles?: string[];
  breakglass: boolean;
  // S242 invitation e-mail (from acp_shared.audit_log): SES accepted / requested / not sent, and the derived state.
  last_invited_at?: string | null;
  last_invite_requested_at?: string | null;
  last_invite_not_sent_at?: string | null;
  invite_status?: InviteStatus | null;
  /** AA-1155 (CIS 462): the CIS custom role code (role then holds its built-in base) and its name; the marker. */
  custom_role?: string | null;
  custom_role_label?: string | null;
  is_business_owner?: boolean;
}

/** AA-1155: every app's built-in and custom roles (the Staff page pickers, Platform › Roles & permissions). */
export function useRolesCatalogue() {
  return useQuery({ queryKey: ["platform-roles"], queryFn: () => apiGet<RolesCatalogue>(ROLES_PATH),
    staleTime: 60_000 });
}

export type InviteStatus = "sent" | "queued" | "not_sent";

type InviteFields = Pick<StaffAccount, "invite_status" | "last_invited_at" | "last_invite_requested_at" | "last_invite_not_sent_at">;

type BookingRoleLabelKey = keyof typeof BOOKING_ROLE.en;
/** English Booking role names (the `en` side of ./messages.ts BOOKING_ROLE), retired roles included. */
export const BOOKING_ROLE_LABEL: Record<BookingRoleLabelKey, string> = BOOKING_ROLE.en;


/** AA-994: the staff label helpers in the UI language — CIS role, Booking role and "Last invited" in words. */
export function useStaffWords() {
  const m = useT(MANAGE);
  const role = useT(RBAC_MESSAGES);
  const booking = useT(BOOKING_ROLE);
  const crm = useT(CRM_ROLE);
  const catalogue = useRolesCatalogue().data;
  return useMemo(() => ({
    // AA-1155: a custom role code shows its name from the roles catalogue
    roleLabel: (r: string) => (isStaffRole(r) ? role(r) : customRoleLabel(catalogue, "cis", r) ?? r),
    bookingRoleLabel: (r: string | null) =>
      (!r ? m("none") : Object.hasOwn(BOOKING_ROLE_LABEL, r) ? booking(r as BookingRoleLabelKey)
        : customRoleLabel(catalogue, "booking", r) ?? r),
    /** AA-1031: CRM roles in words ("Agency admin, Support"), or "None". */
    crmRolesLabel: (roles: readonly string[] | null | undefined) => {
      const known = CRM_ROLES.filter((r) => (roles ?? []).includes(r));
      return known.length ? known.map((r) => crm(r)).join(", ") : m("none");
    },
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
  }), [m, role, booking, crm, catalogue]);
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
  method: "POST" | "PATCH" | "PUT", path: string, okMessage: string, w: WriteWords, body?: unknown,
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
  const m = useT(MANAGE);
  const { roleLabel } = useStaffWords();
  const custom = customRoleOptions(useRolesCatalogue().data, "cis");   // AA-1155: active CIS custom roles
  const listed = (ASSIGNABLE_STAFF_ROLES as readonly string[]).includes(value) || custom.some((c) => c.code === value);
  return (
    <Select id={id} value={value} disabled={disabled} onChange={(e) => onChange(e.target.value)} style={fieldStyle}>
      {/* AA-878 — the read-only viewer is never assignable here; a viewer row shows its role, not selectable.
          AA-1155: an archived custom role shows the same way. */}
      {!listed && value && (
        <option value={value} disabled>{roleLabel(value)}</option>
      )}
      {ASSIGNABLE_STAFF_ROLES.map((r) => <option key={r} value={r}>{roleLabel(r)}</option>)}
      {custom.length > 0 && (
        <optgroup label={m("customRoleGroup")}>
          {custom.map((c) => <option key={c.code} value={c.code}>{c.label}</option>)}
        </optgroup>
      )}
    </Select>
  );
}

function BookingRoleSelect({ id, value, onChange }: { id: string; value: string; onChange: (v: string) => void }) {
  const m = useT(MANAGE);
  const { bookingRoleLabel } = useStaffWords();
  const custom = customRoleOptions(useRolesCatalogue().data, "booking");   // AA-1155: active Booking custom roles
  const unlisted = !!value && !RETIRED_BOOKING_ROLES.includes(value)
    && !(BOOKING_ROLES as readonly string[]).includes(value) && !custom.some((c) => c.code === value);
  return (
    <Select id={id} value={value} onChange={(e) => onChange(e.target.value)} style={fieldStyle}>
      <option value="">{m("noBooking")}</option>
      {/* AA-1150: a retired grant shows its role, never selectable (Sync or a change removes it). AA-1155: so does an
          archived custom role. */}
      {(RETIRED_BOOKING_ROLES.includes(value) || unlisted) && (
        <option value={value} disabled>{bookingRoleLabel(value)}</option>
      )}
      {BOOKING_ROLES.map((r) => <option key={r} value={r}>{bookingRoleLabel(r)}</option>)}
      {custom.length > 0 && (
        <optgroup label={m("customRoleGroup")}>
          {custom.map((c) => <option key={c.code} value={c.code}>{c.label}</option>)}
        </optgroup>
      )}
    </Select>
  );
}

/** AA-1031: CRM roles as a group of kit checkboxes (any number, none = no CRM staff access). */
function CrmRolesField({ idPrefix, value, onChange, disabled }: {
  idPrefix: string;
  value: readonly CrmRole[];
  onChange: (v: CrmRole[]) => void;
  disabled?: boolean;
}) {
  const m = useT(MANAGE);
  const crm = useT(CRM_ROLE);
  const toggle = (r: CrmRole, on: boolean) => {
    const next = new Set(value);
    if (on) next.add(r); else next.delete(r);
    onChange(CRM_ROLES.filter((x) => next.has(x)));
  };
  return (
    <Field as="div" label={m("crmRole")} hint={disabled ? m("crmUnavailable") : m("crmHint")} locked={disabled}>
      <div data-testid={`${idPrefix}-crm`} style={{ display: "flex", gap: "6px 16px", flexWrap: "wrap" }}>
        {CRM_ROLES.map((r) => (
          <Checkbox key={r} id={`${idPrefix}-crm-${r}`} data-testid={`${idPrefix}-crm-${r}`} label={crm(r)}
            checked={value.includes(r)} disabled={disabled} onChange={(e) => toggle(r, e.target.checked)}
            style={{ fontSize: 13.5 }} />
        ))}
      </div>
    </Field>
  );
}

function sameRoles(a: readonly string[], b: readonly string[]): boolean {
  return a.length === b.length && a.every((r) => b.includes(r));
}

// ── Create ───────────────────────────────────────────────────────────────────────────────────────

export function CreateStaffModal({ open, onClose, onDone, crmAvailable = true }: {
  open: boolean;
  onClose: () => void;
  onDone: (o: Outcome) => void;
  /** AA-1031: false until the database update for CRM roles is applied (the list's crm_roles_available). */
  crmAvailable?: boolean;
}) {
  const m = useT(MANAGE);
  const w = useWriteWords();
  const refresh = useRefreshStaff();
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  // AA-1150: least privilege by default (CIS content, no Booking / CRM role, as before); a preset is a deliberate choice.
  const [access, setAccess] = useState<string>("custom");   // a preset code (AA-1155 T14: data) or "custom"
  const { presets } = useActivePresets();
  const [role, setRole] = useState<string>("content");
  const [booking, setBooking] = useState<string>("");
  const [crmRoles, setCrmRoles] = useState<CrmRole[]>([]);
  const [sendInvite, setSendInvite] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const normalized = email.trim().toLowerCase();
  const domainOk = normalized.endsWith(STAFF_DOMAIN) && normalized.length > STAFF_DOMAIN.length;
  const canSubmit = domainOk && name.trim().length > 0 && !busy;

  const reset = () => {
    setEmail(""); setName(""); setAccess("custom"); setRole("content"); setBooking(""); setCrmRoles([]);
    setSendInvite(true); setError(null);
  };
  const close = () => { if (!busy) { reset(); onClose(); } };

  const submit = async () => {
    if (!canSubmit) return;
    setBusy(true);
    setError(null);
    const roles = createRoles(presets.find((p) => p.code === access) ?? null, { role, booking, crm: crmRoles });
    const outcome = await staffWrite("POST", API, "", w,
      {
        email: normalized, display_name: name.trim(), role: roles.role, booking_role: roles.booking,
        send_invite: sendInvite, ...(roles.crm.length ? { crm_roles: roles.crm } : {}),
      });
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
        <CreateRoleFields presets={presets} access={access} onAccess={setAccess} role={role} onRole={setRole} booking={booking}
          onBooking={setBooking} crmRoles={crmRoles} onCrmRoles={setCrmRoles} crmAvailable={crmAvailable} />
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

/** AA-1150: Add staff's role: a people-role preset (its description shown as the hint), or "Custom" with the three
 *  fields. AA-1155 T14: the presets come from the API. */
function CreateRoleFields({ presets, access, onAccess, role, onRole, booking, onBooking, crmRoles, onCrmRoles, crmAvailable }: {
  presets: StaffPreset[];
  access: string;
  onAccess: (v: string) => void;
  role: string;
  onRole: (v: string) => void;
  booking: string;
  onBooking: (v: string) => void;
  crmRoles: CrmRole[];
  onCrmRoles: (v: CrmRole[]) => void;
  crmAvailable: boolean;
}) {
  const m = useT(MANAGE);
  const chosen = presets.find((p) => p.code === access);
  const custom = access === "custom" || !chosen;
  return (
    <>
      <Label text={m("rolesTitle")} htmlFor="staff-create-access" hint={chosen?.description || undefined}>
        <Select id="staff-create-access" data-testid="staff-create-access" value={custom ? "custom" : access}
          style={fieldStyle} onChange={(e) => onAccess(e.target.value)}>
          {presets.map((p) => <option key={p.code} value={p.code}>{p.label}</option>)}
          <option value="custom">{m("customRoles")}</option>
        </Select>
      </Label>
      {custom && (
        <>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 14 }}>
            <Label text={m("cisRole")} htmlFor="staff-create-role">
              <CisRoleSelect id="staff-create-role" value={role} onChange={onRole} />
            </Label>
            <Label text={m("bookingRole")} htmlFor="staff-create-booking">
              <BookingRoleSelect id="staff-create-booking" value={booking} onChange={onBooking} />
            </Label>
          </div>
          <CrmRolesField idPrefix="staff-create" value={crmRoles} onChange={onCrmRoles} disabled={!crmAvailable} />
        </>
      )}
    </>
  );
}

// ── Manage one account (drawer) ──────────────────────────────────────────────────────────────────

type Confirm = null | "disable" | "sign-out" | { preset: StaffPreset };

export function StaffManagePanel({ staff, selfId, onOutcome, crmAvailable = true }: {
  staff: StaffAccount;
  selfId: string | null;
  onOutcome: (o: Outcome) => void;
  /** AA-1031: false until the database update for CRM roles is applied (the list's crm_roles_available). */
  crmAvailable?: boolean;
}) {
  const m = useT(MANAGE);
  const w = useWriteWords();
  const { roleLabel, bookingRoleLabel, crmRolesLabel } = useStaffWords();
  const refresh = useRefreshStaff();
  const savedRole = cisRoleOf(staff);   // AA-1155: the custom role code of a custom holder
  const [role, setRole] = useState(savedRole);
  const [booking, setBooking] = useState(staff.booking_role ?? "");
  const savedCrm = crmRolesOf(staff);
  const [crmRoles, setCrmRoles] = useState<CrmRole[]>(savedCrm);
  const [busy, setBusy] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<Confirm>(null);
  const [showAll, setShowAll] = useState(false);   // AA-1150: every other role is behind "Show all roles"

  const isSelf = !!selfId && selfId === staff.id;
  const base = `${API}/${encodeURIComponent(staff.id)}`;

  if (staff.breakglass) {
    return (
      <Note>{m("breakGlass")}</Note>
    );
  }

  const run = async (key: string, method: "POST" | "PATCH" | "PUT", path: string, ok: string, body?: unknown) => {
    setBusy(key);
    const outcome = await staffWrite(method, path, ok, w, body);
    setBusy(null);
    setConfirm(null);
    await refresh();
    onOutcome(outcome);
  };

  const roleChanged = role !== savedRole;
  const bookingChanged = booking !== (staff.booking_role ?? "");
  const crmChanged = !sameRoles(crmRoles, savedCrm);
  const saveRoles = () => {
    const body: Record<string, string | string[] | null> = {};
    if (roleChanged) body.role = role;
    if (bookingChanged) body.booking_role = booking || null;
    if (crmChanged) body.crm_roles = crmRoles;
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
      {/* AA-1150 / AA-1155 T14: the people-role presets (data). Every viewer of this panel is a platform owner
          (canWriteStaff) and the backend re-checks it. */}
      {staff.is_active && (
        <PeoplePresets staff={staff} role={savedRole} isSelf={isSelf} busy={!!busy} crmAvailable={crmAvailable}
          onApply={(preset) => setConfirm({ preset })} />
      )}
      <div>
        <Button size="sm" variant="ghost" testId="staff-show-all-roles" aria-expanded={showAll}
          onClick={() => setShowAll((v) => !v)}>
          {showAll ? m("hideAllRoles") : m("showAllRoles")}
        </Button>
      </div>
      {showAll && (
        <div data-testid="staff-all-roles" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))", gap: 12 }}>
            <Label text={m("cisRole")} htmlFor={`staff-role-${staff.id}`}
              hint={isSelf ? m("ownRole") : undefined}>
              <CisRoleSelect id={`staff-role-${staff.id}`} value={role} onChange={setRole} disabled={isSelf} />
            </Label>
            <Label text={m("bookingRole")} htmlFor={`staff-booking-${staff.id}`}>
              <BookingRoleSelect id={`staff-booking-${staff.id}`} value={booking} onChange={setBooking} />
            </Label>
          </div>
          <CrmRolesField idPrefix={`staff-${staff.id}`} value={crmRoles} onChange={setCrmRoles} disabled={!crmAvailable} />
          <div>
            <Button size="sm" variant="primary" onClick={saveRoles}
              disabled={(!roleChanged && !bookingChanged && !crmChanged) || !!busy}>
              {busy === "roles" ? m("saving") : m("saveRoles")}
            </Button>
          </div>
          <div style={{ fontSize: 12, color: K.muted, lineHeight: 1.5 }}>
            {m("eachChange")}
            {roleChanged && m("cisChange", roleLabel(savedRole), roleLabel(role))}
            {bookingChanged && m("bookingChange", bookingRoleLabel(staff.booking_role), bookingRoleLabel(booking || null))}
            {crmChanged && m("crmChange", crmRolesLabel(savedCrm), crmRolesLabel(crmRoles))}
          </div>
        </div>
      )}
      <BusinessOwnerControl staff={staff} busy={busy} onRun={(ok, body) => run("business-owner", "PUT",
        `${base}/business-owner`, ok, body)} />
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
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
          <Button size="sm" variant="ghost" disabled={!!busy} testId="staff-resend-invite"
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
      <PresetConfirm staff={staff} role={savedRole} preset={isPreset(confirm) ? confirm.preset : null}
        busy={busy === "preset"} onClose={() => setConfirm(null)}
        onConfirm={(p, done) => void run("preset", "PATCH", `${base}/roles`, done, { preset: p.code })} />
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

function isPreset(c: Confirm): c is { preset: StaffPreset } {
  return typeof c === "object" && c !== null;
}

function Note({ children }: { children: React.ReactNode }) {
  return (
    <div style={{ fontSize: 12.5, color: K.muted, lineHeight: 1.5, padding: "8px 10px", background: K.line2,
      borderRadius: RADIUS.sm }}>
      {children}
    </div>
  );
}
