// lib/tenantMembers.ts — AA-1031 (S264): the pure parts of the Tenant 360 Members tab (CIS Content studio memberships,
// shared.tenant_members). The routes are the existing staff ones in api/routers/admin_tenant360.py (AA-978, AA-979 P2b):
//   GET   /admin/tenants/{id}/members                                     every membership, removed ones last
//   POST  /admin/tenants/{id}/members/invitations                         {email, content_studio_role, locale}, 201
//   POST  /admin/tenants/{id}/members/{membership_id}/invitation/send     {email?, locale}: send an invited row again
//         (AA-1035: no email = to the address the CRM kept; a new address only for roles that may re-address)
//   PATCH /admin/tenants/{id}/members/{membership_id}                     {content_studio_role}
//   POST  /admin/tenants/{id}/members/{membership_id}/remove              final
//   GET   /admin/tenants/{id}/members/access                              which of these writes this session may use
// Writes need tenant.write (admin); GET .../members/access (AA-1031 F3) says which of them THIS session may use, so a
// read-only session (the Dev E2E admin, whose role cookie says "admin") sees no write control. CIS never stores the
// e-mail: the CRM delivers the AA system mail once, so a send again asks for the address again. No CRM role anywhere
// (root ADR 0010a decision 3: CIS never grants one).
// No React here, so node --test can load it (lib/tenantMembers.test.mjs).

export const STUDIO_ROLES = ["owner", "editor", "viewer"] as const;
export type StudioRole = (typeof STUDIO_ROLES)[number];
export type MemberStatus = "invited" | "active" | "removed";
export const INVITE_LOCALES = ["en", "vi"] as const;
export type InviteLocale = (typeof INVITE_LOCALES)[number];

/** One row of GET /admin/tenants/{id}/members (services/tenant_membership/writer.py staff_row). */
export interface TenantMember {
  membership_id: string;
  /** sha256 of (iss, sub), 12 hex; null while invited (no identity bound yet). */
  identity_fingerprint: string | null;
  content_studio_role: StudioRole;
  status: MemberStatus;
  invitation_id: string | null;
  created_at: string;
  created_by: string | null;
  updated_at: string;
  removed_at: string | null;
  removed_by: string | null;
}

export interface TenantMembers {
  tenant_id: string;
  members: TenantMember[];
  note?: string;
}

/** The CRM delivery of an invitation (never raises on the CIS side: a failed relay is 201 + status "failed"). */
export interface InvitationDelivery {
  status: "sent" | "failed";
  crm_status: number;
  expires_at?: string | null;
  detail?: string | null;
  message?: string | null;
}

export interface InvitationResult {
  tenant_id: string;
  member: TenantMember;
  delivery: InvitationDelivery;
}

export interface MemberWriteResult {
  tenant_id: string;
  member: TenantMember;
  changed: boolean;
}

export const membersPath = (tenantId: string) => `/api/admin/tenants/${encodeURIComponent(tenantId)}/members`;
export const invitationsPath = (tenantId: string) => `${membersPath(tenantId)}/invitations`;
export const memberPath = (tenantId: string, membershipId: string) =>
  `${membersPath(tenantId)}/${encodeURIComponent(membershipId)}`;
export const sendAgainPath = (tenantId: string, membershipId: string) =>
  `${memberPath(tenantId, membershipId)}/invitation/send`;
export const removePath = (tenantId: string, membershipId: string) => `${memberPath(tenantId, membershipId)}/remove`;
/** GET: which member write controls this session may use on the tenant (AA-1031 F3, `MembersAccess`). */
export const membersAccessPath = (tenantId: string) => `${membersPath(tenantId)}/access`;

// Mirrors api/routers/admin_tenant360.py _EMAIL and the MemberInvite length limits (6..254).
const EMAIL = /^[^@\s]{1,64}@[^@\s]{1,189}\.[^@\s]{2,63}$/;

/** Would the API accept this address? (trimmed; the API checks again) */
export function isInviteEmail(raw: string): boolean {
  const v = raw.trim();
  return v.length >= 6 && v.length <= 254 && EMAIL.test(v);
}

export function isStudioRole(v: string): v is StudioRole {
  return (STUDIO_ROLES as readonly string[]).includes(v);
}

/** Badge tone of a membership status. */
export function statusTone(status: string): "success" | "warning" | "neutral" {
  return status === "active" ? "success" : status === "invited" ? "warning" : "neutral";
}

/** Which row actions a membership offers (removed is final: nothing). */
export function memberActions(m: Pick<TenantMember, "status" | "invitation_id">): {
  changeRole: boolean; sendAgain: boolean; remove: boolean;
} {
  const live = m.status !== "removed";
  return { changeRole: live, sendAgain: m.status === "invited" && !!m.invitation_id, remove: live };
}

/** Is this membership the agency's last ACTIVE owner? Removing it (or changing its role) locks the agency in the
 *  partner CRM until an owner recovery, so the page warns first. */
export function isLastActiveOwner(members: readonly TenantMember[], membershipId: string): boolean {
  const owners = members.filter((m) => m.status === "active" && m.content_studio_role === "owner");
  return owners.length === 1 && owners[0].membership_id === membershipId;
}

/** Live (invited + active) memberships, for the tab label. */
export function liveCount(members: readonly TenantMember[]): number {
  return members.filter((m) => m.status !== "removed").length;
}

/** Was the e-mail delivered? A failed relay keeps the invited row: the staff member sends it again. */
export function deliveryOk(d: InvitationDelivery | null | undefined): boolean {
  return d?.status === "sent";
}

/** The CRM's refusal of a failed delivery, as one line ("message (code)", the code alone, or the HTTP status). */
export function deliveryProblem(d: InvitationDelivery | null | undefined): string {
  if (!d || d.status === "sent") return "";
  const code = d.detail ? String(d.detail) : "";
  const msg = d.message ? String(d.message) : "";
  if (msg && code) return `${msg} (${code})`;
  return msg || code || `HTTP ${d.crm_status}`;
}

// ── AA-1031 (S264 live check F3): which member writes this session may use ────────────────────────────────────────

export const MEMBER_WRITE_ACTIONS = ["invite", "send_again", "change_role", "remove"] as const;
export type MemberWriteAction = (typeof MEMBER_WRITE_ACTIONS)[number];

/** GET /admin/tenants/{id}/members/access (api/routers/admin_tenant360.py members_access): each action is the API's
 *  own route policy for this caller; invite_roles are the roles an invitation (or a send again) may carry. */
export interface MembersAccess {
  tenant_id?: string;
  read_only: boolean;
  actions: Partial<Record<MemberWriteAction, boolean>>;
  invite_roles?: string[];
  /** AA-1035: may change the role of / remove an owner row (and pick owner as a new role). Missing = the older API,
   *  which had no such limit. */
  owner_writes?: boolean;
  /** AA-1035 close-out: how a send again may address the mail (the CRM's kept address / a typed new one). Missing =
   *  the older API: a typed address only. */
  send_again_to?: { same_address?: boolean; new_address?: boolean };
}

/** What the page may offer. `settled` is false while the access read is in flight (nothing is offered then). */
export interface MemberPermissions {
  settled: boolean;
  invite: boolean;
  sendAgain: boolean;
  changeRole: boolean;
  remove: boolean;
  inviteRoles: StudioRole[];
  /** AA-1035: change role / remove on an owner row and "owner" in the role dialog (tenant.write only). */
  ownerWrites: boolean;
  /** AA-1035 close-out: a send again may go to the address the CRM kept (no address typed). */
  sendAgainSame: boolean;
  /** AA-1035 close-out: a send again may go to a newly typed address (never support). */
  sendAgainNew: boolean;
}

/**
 * The server's answer decides. While it loads, nothing is offered (no write control flashes for a read-only
 * session). Only when the read FAILED does the role decide (`roleCanWrite`: lib/rbac can(role, "tenant.write")); the
 * API still refuses what it must.
 */
export function memberPermissions(
  access: MembersAccess | undefined, failed: boolean, roleCanWrite: boolean,
): MemberPermissions {
  if (access) {
    const a = access.actions ?? {};
    const invite = a.invite === true;
    const sendAgain = a.send_again === true;
    const roles = access.invite_roles === undefined ? [...STUDIO_ROLES] : access.invite_roles.filter(isStudioRole);
    const changeRole = a.change_role === true;
    const remove = a.remove === true;
    const to = access.send_again_to;
    return {
      settled: true, invite, sendAgain, changeRole, remove,
      inviteRoles: invite || sendAgain ? roles : [],
      ownerWrites: (changeRole || remove) && access.owner_writes !== false,
      sendAgainSame: sendAgain && to?.same_address === true,
      sendAgainNew: sendAgain && (to === undefined || to.new_address === true),
    };
  }
  const w = failed && roleCanWrite;
  return {
    settled: failed, invite: w, sendAgain: w, changeRole: w, remove: w, inviteRoles: w ? [...STUDIO_ROLES] : [],
    ownerWrites: w, sendAgainSame: false, sendAgainNew: w,
  };
}

/** AA-1035 close-out: the body of a send again. `email` empty (or a same-address send) = the address the CRM kept. */
export function sendAgainBody(sameAddress: boolean, email: string, locale: string): { email?: string; locale: string } {
  const typed = email.trim();
  return sameAddress || !typed ? { locale } : { email: typed, locale };
}

/** May the page offer any write at all? (the "read only" note shows when the answer is settled and this is false) */
export function anyMemberWrite(p: MemberPermissions): boolean {
  return p.invite || p.sendAgain || p.changeRole || p.remove;
}

/** The row actions of one membership for this session: what the row allows (memberActions) AND what the session may
 *  use. A send again re-seals the invitation with its stored role, so it needs that role in `inviteRoles`; an owner
 *  row changes or goes only with `ownerWrites` (AA-1035). */
export function memberRowActions(
  m: Pick<TenantMember, "status" | "invitation_id" | "content_studio_role">, p: MemberPermissions,
): { changeRole: boolean; sendAgain: boolean; remove: boolean } {
  const a = memberActions(m);
  const ownerOk = m.content_studio_role !== "owner" || p.ownerWrites;
  return {
    changeRole: a.changeRole && p.changeRole && ownerOk,
    sendAgain: a.sendAgain && p.sendAgain && p.inviteRoles.includes(m.content_studio_role),
    remove: a.remove && p.remove && ownerOk,
  };
}

/** The roles the role dialog may offer this session (AA-1035: owner only with `ownerWrites`). */
export function roleChoices(p: MemberPermissions): StudioRole[] {
  return STUDIO_ROLES.filter((r) => r !== "owner" || p.ownerWrites);
}

/** May this session send a NEW invitation with `role`? (the agency drawer's studio box invites as owner) */
export function mayInviteAs(p: MemberPermissions, role: StudioRole): boolean {
  return p.invite && p.inviteRoles.includes(role);
}
