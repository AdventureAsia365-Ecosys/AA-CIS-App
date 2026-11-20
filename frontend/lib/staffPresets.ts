// lib/staffPresets.ts — AA-1150 (lean staff roles step 1): the Staff page's people-role presets and the Booking roles
// the directory grants, as data (no React, so lib/staffPresets.test.mjs loads it). The backend is the authority:
// api/services/staff_directory.py PRESETS / BOOKING_ROLES / RETIRED_BOOKING_ROLES, re-checked on every write.

/** The Booking roles the directory grants (backend BOOKING_ROLES). content_creator is retired. */
export const BOOKING_ROLES = ["admin", "sales", "contracting", "finance"] as const;
export type BookingRole = (typeof BOOKING_ROLES)[number];

/** Booking roles never granted again (backend RETIRED_BOOKING_ROLES): an old grant shows as retired, Sync removes it. */
export const RETIRED_BOOKING_ROLES: readonly string[] = ["content_creator"];

/** The CRM staff roles in display order (backend staff_directory.CRM_ROLES). */
export const CRM_ROLES = ["platform_admin", "ops", "support"] as const;
export type CrmRole = (typeof CRM_ROLES)[number];

/** The three people-role presets in display order. "Administrator" is the backend's platform_owner preset. */
export const PEOPLE_PRESETS = ["platform_owner", "business_owner", "content_ops"] as const;
export type PeoplePreset = (typeof PEOPLE_PRESETS)[number];

export interface PresetRoles {
  role: string;
  booking: string | null;
  /** null = the preset leaves the CRM roles as they are (Business owner). */
  crm: CrmRole[] | null;
}

/** What each preset sets (backend change_roles): Administrator adds CRM Agency admin to the CRM roles held;
 *  Content + Product & operations gives no Booking role until custom roles exist (tech lead decision Q1, S285). */
export const PEOPLE_PRESET_ROLES: Record<PeoplePreset, PresetRoles> = {
  platform_owner: { role: "admin", booking: "admin", crm: ["platform_admin"] },
  business_owner: { role: "admin", booking: "admin", crm: null },
  content_ops: { role: "content_lead", booking: null, crm: [] },
};

interface Roles {
  role: string;
  booking_role: string | null;
  crm_roles?: string[];
}

/** The account's CRM roles in CRM_ROLES order, unknown codes dropped. */
export function crmRolesOf(s: Pick<Roles, "crm_roles">): CrmRole[] {
  const held = new Set(s.crm_roles ?? []);
  return CRM_ROLES.filter((r) => held.has(r));
}

/** The "Administrator" (platform_owner) preset is in place: CIS admin, Booking admin and CRM Agency admin. */
export function hasPlatformOwnerRoles(s: Roles): boolean {
  return s.role === "admin" && s.booking_role === "admin" && crmRolesOf(s).includes("platform_admin");
}

/** The people-role preset the account's roles match, or null. Administrator is tried first: it also has Business
 *  owner's CIS and Booking roles. */
export function peoplePresetOf(s: Roles): PeoplePreset | null {
  if (hasPlatformOwnerRoles(s)) return "platform_owner";
  if (s.role === "admin" && s.booking_role === "admin") return "business_owner";
  if (s.role === "content_lead" && !s.booking_role && crmRolesOf(s).length === 0) return "content_ops";
  return null;
}

/** The roles a new account gets in Add staff (the create API has no preset field): the preset's (a preset that keeps
 *  the CRM roles gives none to a new account), or the custom fields. */
export function createRoles(
  access: PeoplePreset | "custom",
  custom: { role: string; booking: string; crm: CrmRole[] },
): { role: string; booking: string | null; crm: CrmRole[] } {
  if (access === "custom") return { role: custom.role, booking: custom.booking || null, crm: custom.crm };
  const p = PEOPLE_PRESET_ROLES[access];
  return { role: p.role, booking: p.booking, crm: p.crm ?? [] };
}
