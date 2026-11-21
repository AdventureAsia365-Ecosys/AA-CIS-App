// lib/staffRoleEditor.ts — AA-1155 (spec tasks T13 / T14): the data shapes and pure helpers of the Staff page's CIS
// role editor and people-role preset editor (no React, so lib/staffRoleEditor.test.mjs loads it). The backend is the
// authority (api/services/staff_role_editor.py, api/services/staff_presets.py) and re-checks every write: owners
// only, approve-class and unknown capabilities refused (400), audited before and after.

import type { StaffPreset } from "./staffPresets";

/** Backend routes through the BFF. */
export const CIS_ROLE_EDITOR_PATH = "/api/admin/platform/roles/cis";

/** A custom role code (CIS 462 staff_role_code_chk and the Booking table: one pattern in both apps). */
export const CUSTOM_ROLE_CODE_RE = /^custom_[a-z][a-z0-9_]{1,46}$/;
/** A preset code (CIS 465 staff_role_preset_code_chk). */
export const PRESET_CODE_RE = /^[a-z][a-z0-9_]{1,47}$/;

export interface EditorRole {
  code: string;
  label: string;
  based_on: string;
  archived: boolean;
  capabilities: string[];
  holders: number;
}

export interface EditorBuiltin {
  code: string;
  label: string;
  capabilities: string[];
  assignable: boolean;
  clonable: boolean;
}

/** GET /api/admin/platform/roles/cis. */
export interface RoleEditorView {
  builtin: EditorBuiltin[];
  custom: EditorRole[];
  custom_available: boolean;
  capabilities: { name: string; assignable: boolean }[];
  can_edit: boolean;
}

/** The capabilities a custom role may hold, grouped by area (the part before the first dot), areas and names sorted. */
export function assignableByArea(view: Pick<RoleEditorView, "capabilities"> | undefined): [string, string[]][] {
  const groups = new Map<string, string[]>();
  for (const c of view?.capabilities ?? []) {
    if (!c.assignable) continue;
    const area = c.name.split(".", 1)[0];
    groups.set(area, [...(groups.get(area) ?? []), c.name]);
  }
  return [...groups.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([a, names]) => [a, names.sort()]);
}

/** The capabilities a new role starts with: the built-in role's that a custom role may hold (backend clone_of). */
export function cloneCapabilities(view: RoleEditorView | undefined, basedOn: string): string[] {
  const allowed = new Set((view?.capabilities ?? []).filter((c) => c.assignable).map((c) => c.name));
  const base = view?.builtin.find((b) => b.code === basedOn);
  return (base?.capabilities ?? []).filter((c) => allowed.has(c)).sort();
}

/** The PATCH body of a custom role edit: only what changed (label trimmed, capabilities as the full sorted set). */
export function roleEditBody(before: EditorRole, label: string, capabilities: readonly string[]):
  { label?: string; capabilities?: string[] } {
  const body: { label?: string; capabilities?: string[] } = {};
  if (label.trim() !== before.label) body.label = label.trim();
  const next = [...new Set(capabilities)].sort();
  const held = [...before.capabilities].sort();
  if (next.length !== held.length || next.some((c, i) => c !== held[i])) body.capabilities = next;
  return body;
}

/** A role form is complete enough to send (the backend checks the rest). */
export function roleFormOk(f: { code?: string; label: string; capabilities: readonly string[] }): boolean {
  const label = f.label.trim();
  return (f.code === undefined || CUSTOM_ROLE_CODE_RE.test(f.code)) && label.length >= 1 && label.length <= 80
    && f.capabilities.length > 0;
}

// ── Presets ─────────────────────────────────────────────────────────────────────────────────────────────────────

/** The editable fields of a preset (backend staff_presets.EDITABLE); never an owner flag. */
export const PRESET_FIELDS = [
  "label", "description", "cis_role", "booking_mode", "booking_role", "crm_mode", "crm_roles", "additive_only",
  "cis_keep_roles", "booking_keep_roles", "sort_order", "archived",
] as const;
export type PresetField = (typeof PRESET_FIELDS)[number];
export type PresetForm = Omit<StaffPreset, "code"> & { code: string };

/** An empty form for a new preset. */
export function blankPreset(sortOrder = 100): PresetForm {
  return {
    code: "", label: "", description: "", cis_role: null, booking_mode: "keep", booking_role: null, crm_mode: "keep",
    crm_roles: [], additive_only: false, cis_keep_roles: [], booking_keep_roles: [], sort_order: sortOrder,
    archived: false,
  };
}

/** The form made consistent before it is sent: a Booking role only with mode "set", CRM roles only when not kept,
 *  keep lists only on an additive-only preset (the backend refuses the other combinations with 400). */
export function normalizePreset(f: PresetForm): PresetForm {
  const additive = f.additive_only;
  return {
    ...f,
    label: f.label.trim(),
    description: f.description.trim(),
    booking_role: f.booking_mode === "set" ? f.booking_role : null,
    crm_roles: f.crm_mode === "keep" ? [] : f.crm_roles,
    cis_keep_roles: additive ? f.cis_keep_roles : [],
    booking_keep_roles: additive ? f.booking_keep_roles : [],
  };
}

function sameValue(a: unknown, b: unknown): boolean {
  if (Array.isArray(a) && Array.isArray(b)) return a.length === b.length && a.every((v, i) => v === b[i]);
  return a === b;
}

/** The PATCH body of a preset edit: the fields that changed. */
export function presetEditBody(before: StaffPreset, form: PresetForm): Partial<Record<PresetField, unknown>> {
  const next = normalizePreset(form);
  const body: Partial<Record<PresetField, unknown>> = {};
  for (const k of PRESET_FIELDS) if (!sameValue(before[k], next[k])) body[k] = next[k];
  return body;
}

/** The POST body of a new preset (code + every field). */
export function presetCreateBody(form: PresetForm): Record<string, unknown> {
  const next = normalizePreset(form);
  const body: Record<string, unknown> = { code: next.code };
  for (const k of PRESET_FIELDS) body[k] = next[k];
  return body;
}

export type PresetProblem = "code" | "label" | "bookingRole" | "crmRoles" | "additive" | "empty";

/** The rules a preset form must meet, in the order they are reported (the same as the backend's checks). */
const PRESET_RULES: [PresetProblem, (n: PresetForm, isNew: boolean) => boolean][] = [
  ["code", (n, isNew) => !isNew || PRESET_CODE_RE.test(n.code)],
  ["label", (n) => n.label.length >= 1 && n.label.length <= 80],
  ["bookingRole", (n) => n.booking_mode !== "set" || !!n.booking_role],
  ["crmRoles", (n) => n.crm_mode !== "add" || n.crm_roles.length > 0],
  ["additive", (n) => !n.additive_only || (n.booking_mode !== "clear" && n.crm_mode !== "replace")],
  ["empty", (n) => n.cis_role !== null || n.booking_mode !== "keep" || n.crm_mode !== "keep"],
];

/** Why a preset form cannot be sent yet, or null (the same rules as the backend, so the owner sees them first). */
export function presetFormProblem(f: PresetForm, isNew: boolean): PresetProblem | null {
  const n = normalizePreset(f);
  return PRESET_RULES.find(([, ok]) => !ok(n, isNew))?.[0] ?? null;
}
