// lib/staffPresets.ts — the Staff page's people-role presets and the Booking / CRM roles the directory grants (no
// React, so lib/staffPresets.test.mjs loads it). The backend is the authority: api/services/staff_directory.py
// BOOKING_ROLES / CRM_ROLES and api/services/staff_presets.py RETIRED_BOOKING_ROLES, re-checked on every write.
//
// AA-1155 (spec task T14, root spec R5.7): the presets are data (shared.staff_role_preset, CIS 465), read from
// GET /api/admin/platform/presets and edited there by an owner. No preset is named in this file: it only holds the
// shape and the pure planning helpers that mirror api/services/staff_presets.plan() for the Staff page's "Current"
// mark, the confirm text and Add staff (the create API takes roles, not a preset).

/** The Booking roles the directory grants (backend BOOKING_ROLES). content_creator is retired. */
export const BOOKING_ROLES = ["admin", "sales", "contracting", "finance"] as const;
export type BookingRole = (typeof BOOKING_ROLES)[number];

/** Booking roles never granted again (backend RETIRED_BOOKING_ROLES): an old grant shows as retired, Sync removes it. */
export const RETIRED_BOOKING_ROLES: readonly string[] = ["content_creator"];

/** The CRM staff roles in display order (backend staff_directory.CRM_ROLES). */
export const CRM_ROLES = ["platform_admin", "ops", "support"] as const;
export type CrmRole = (typeof CRM_ROLES)[number];

/** Backend route of the presets (GET list, POST create, PATCH /{code}). */
export const PRESETS_PATH = "/api/admin/platform/presets";

/** The CIS role an additive-only preset may raise (backend CIS_EMPTY_ROLES: the read-only role loses nothing). */
export const CIS_EMPTY_ROLES: readonly string[] = ["viewer"];

export type BookingMode = "keep" | "set" | "clear";
export type CrmMode = "keep" | "replace" | "add";
export const BOOKING_MODES: readonly BookingMode[] = ["keep", "set", "clear"];
export const CRM_MODES: readonly CrmMode[] = ["keep", "replace", "add"];

/** One preset row (backend staff_presets.Preset.as_dict). */
export interface StaffPreset {
  code: string;
  label: string;
  description: string;
  /** The CIS role it grants (built-in or custom code); null = kept. */
  cis_role: string | null;
  booking_mode: BookingMode;
  booking_role: string | null;
  crm_mode: CrmMode;
  crm_roles: string[];
  additive_only: boolean;
  cis_keep_roles: string[];
  booking_keep_roles: string[];
  sort_order: number;
  archived: boolean;
}

/** GET /api/admin/platform/presets. `available` is false until the database update that stores them is applied. */
export interface PresetList {
  presets: StaffPreset[];
  available: boolean;
  can_edit: boolean;
}

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

/** The presets the Staff page offers: active ones, by sort order then label. */
export function activePresets(presets: readonly StaffPreset[] | undefined): StaffPreset[] {
  return [...(presets ?? [])].filter((p) => !p.archived)
    .sort((a, b) => a.sort_order - b.sort_order || a.label.localeCompare(b.label) || a.code.localeCompare(b.code));
}

/** What a preset changes on an account (backend staff_presets.plan). `undefined` = that app's role is kept; `booking`
 *  null = the grant removed. `lost` names each held role an additive-only preset would have to replace ("cis:<code>",
 *  "booking:<code>"); the backend refuses those with 409 preset_would_replace. */
export interface PresetPlan {
  role?: string;
  booking?: string | null;
  crm?: CrmRole[];
  lost: { app: "cis" | "booking"; role: string }[];
}

/** The roles `p` sets on an account holding `role` (its CIS code, a custom code included), `booking_role` and
 *  `crm_roles`. Pure, the same rules as the backend. */
export function presetPlan(p: StaffPreset, s: Roles): PresetPlan {
  const out: PresetPlan = { lost: [] };
  if (p.cis_role !== null) {
    const role = planSlot(p, p.cis_role, s.role || null, p.cis_keep_roles, CIS_EMPTY_ROLES);
    if (role.lost) out.lost.push({ app: "cis", role: role.lost });
    if (role.set !== undefined) out.role = role.set;
  }
  const booking = planBooking(p, s);
  if (booking.lost) out.lost.push({ app: "booking", role: booking.lost });
  if ("set" in booking) out.booking = booking.set;
  const crm = planCrm(p, s);
  if (crm !== undefined) out.crm = crm;
  return out;
}

/** One app's planned role (backend staff_presets._slot): `next` for a plain preset; for an additive-only one kept
 *  when the held role is listed (or is `next`), `next` in an empty slot (none, or one of `empty`), else kept and
 *  reported as `lost`. */
function planSlot(p: StaffPreset, next: string, old: string | null, keep: readonly string[], empty: readonly string[]):
  { set?: string; lost?: string } {
  if (!p.additive_only) return { set: next };
  if (old !== null && (keep.includes(old) || old === next)) return {};
  if (old === null || empty.includes(old)) return { set: next };
  return { lost: old };
}

/** The Booking grant a preset sets: cleared (set null), a role (planSlot; a retired grant is an empty slot), or kept. */
function planBooking(p: StaffPreset, s: Roles): { set?: string | null; lost?: string } {
  if (p.booking_mode === "clear") return { set: null };
  if (p.booking_mode !== "set" || p.booking_role === null) return {};
  return planSlot(p, p.booking_role, s.booking_role ?? null, p.booking_keep_roles, RETIRED_BOOKING_ROLES);
}

/** The CRM roles a preset sets: undefined = kept; replace = its set; add = its set added to the roles held. */
function planCrm(p: StaffPreset, s: Roles): CrmRole[] | undefined {
  if (p.crm_mode === "keep") return undefined;
  const held = new Set<string>([...(p.crm_mode === "add" ? crmRolesOf(s) : []), ...p.crm_roles]);
  return CRM_ROLES.filter((r) => held.has(r));
}

/** The roles that actually change (a planned value equal to the held one is not a change). */
export function planChanges(plan: PresetPlan, s: Roles): {
  role?: { from: string; to: string };
  booking?: { from: string | null; to: string | null };
  crm?: { from: CrmRole[]; to: CrmRole[] };
} {
  const out: ReturnType<typeof planChanges> = {};
  if (plan.role !== undefined && plan.role !== s.role) out.role = { from: s.role, to: plan.role };
  if (plan.booking !== undefined && plan.booking !== (s.booking_role ?? null)) {
    out.booking = { from: s.booking_role ?? null, to: plan.booking };
  }
  const held = crmRolesOf(s);
  if (plan.crm !== undefined && (plan.crm.length !== held.length || plan.crm.some((r) => !held.includes(r)))) {
    out.crm = { from: held, to: plan.crm };
  }
  return out;
}

/** Applying `p` changes nothing on this account (and replaces nothing). */
export function presetInPlace(p: StaffPreset, s: Roles): boolean {
  const plan = presetPlan(p, s);
  return plan.lost.length === 0 && Object.keys(planChanges(plan, s)).length === 0;
}

/** The first active preset already in place on the account (the "Current" mark), or null. */
export function currentPreset(presets: readonly StaffPreset[] | undefined, s: Roles): StaffPreset | null {
  return activePresets(presets).find((p) => presetInPlace(p, s)) ?? null;
}

/** Why `p` cannot be applied to this row, or null: it would demote oneself (the backend's self-demote check), replace
 *  a held role (additive only), or write CRM roles while the CRM table is not available. */
export function presetBlockedWhy(p: StaffPreset, s: Roles, isSelf: boolean, crmAvailable: boolean):
  "ownRole" | "wouldReplace" | "crmUnavailable" | null {
  const plan = presetPlan(p, s);
  if (isSelf && plan.role !== undefined && plan.role !== s.role && plan.role !== "admin") return "ownRole";
  if (plan.lost.length) return "wouldReplace";
  if (p.crm_mode !== "keep" && !crmAvailable) return "crmUnavailable";
  return null;
}

/** The roles a new account gets in Add staff (the create API takes roles): the preset's plan on an empty account
 *  (a role the preset keeps comes from the form), or the custom fields. */
export function createRoles(
  preset: StaffPreset | null,
  custom: { role: string; booking: string; crm: CrmRole[] },
): { role: string; booking: string | null; crm: CrmRole[] } {
  if (preset === null) return { role: custom.role, booking: custom.booking || null, crm: custom.crm };
  const plan = presetPlan(preset, { role: "", booking_role: null, crm_roles: [] });
  return { role: plan.role ?? custom.role, booking: plan.booking ?? null, crm: plan.crm ?? [] };
}
