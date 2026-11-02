// lib/tenantMembers.ts — AA-1031 (S264): the pure parts of the Tenant 360 Members tab (CIS Content studio memberships,
// shared.tenant_members). The routes are the existing staff ones in api/routers/admin_tenant360.py (AA-978, AA-979 P2b):
//   GET   /admin/tenants/{id}/members                                     every membership, removed ones last
//   POST  /admin/tenants/{id}/members/invitations                         {email, content_studio_role, locale}, 201
//   POST  /admin/tenants/{id}/members/{membership_id}/invitation/send     {email, locale}: send an invited row again
//   PATCH /admin/tenants/{id}/members/{membership_id}                     {content_studio_role}
//   POST  /admin/tenants/{id}/members/{membership_id}/remove              final
// Writes need tenant.write (admin). CIS never stores the e-mail: the CRM delivers the AA system mail once, so a send
// again asks for the address again. No CRM role anywhere (root ADR 0010a decision 3: CIS never grants one).
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
