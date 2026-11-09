// frontend/lib/tenantAssignments.ts — AA-1035 R2 slice 2: the pure parts of Tenants › Assignments (who looks after
// which tenant: owner, backup, support; root ADR 0019 d4).
//
// Reads GET /admin/tenant-assignments (every tenant of the caller's scope with its assignments and the pool flag) and,
// for whoever assigns (tenant.assign: admin), GET /admin/tenant-assignments/staff. Writes stay the per-tenant routes
// PUT / DELETE /admin/tenants/{tenant_id}/assignments/{admin_user_id} (audited). Dependency-free.

export const ASSIGNMENTS_API = "/api/admin/tenant-assignments";
export const ASSIGNABLE_STAFF_API = "/api/admin/tenant-assignments/staff";
export const ASSIGNMENTS_QUERY_KEY = ["tenant-assignments"] as const;

export const ASSIGNMENT_KINDS = ["owner", "backup", "support"] as const;
export type AssignmentKind = (typeof ASSIGNMENT_KINDS)[number];

export interface Assignment {
  admin_user_id: string;
  kind: AssignmentKind;
  display_name: string | null;
  staff_role: string;
  is_active: boolean;
  created_at: string | null;
  created_by: string;
}

export interface TenantAssignments {
  tenant_id: string;
  name: string;
  slug: string;
  kind: string;
  segment: string | null;
  status: string;
  in_pool: boolean;
  assignments: Assignment[];
}

export interface AssignmentsResp {
  tenants: TenantAssignments[];
  can_assign: boolean;
  scope: "all" | "scoped";
}

export interface AssignableStaff {
  admin_user_id: string;
  display_name: string | null;
  email: string | null;
  role: string;
}

export function assignmentPath(tenantId: string, adminUserId: string): string {
  return `/api/admin/tenants/${encodeURIComponent(tenantId)}/assignments/${encodeURIComponent(adminUserId)}`;
}

/** The staff members of one kind on a tenant, in the API's order. */
export function ofKind(t: Pick<TenantAssignments, "assignments">, kind: AssignmentKind): Assignment[] {
  return t.assignments.filter((a) => a.kind === kind);
}

/** How a staff member is printed: the display name, else the first 8 characters of the id. */
export function staffLabel(a: { display_name?: string | null; admin_user_id: string }): string {
  return a.display_name?.trim() || a.admin_user_id.slice(0, 8);
}

export type AssignmentFilter = "all" | "no_owner" | "pool";

/** The rows a filter keeps: every tenant, tenants without an owner, or the support pool. */
export function filterTenants(rows: TenantAssignments[], filter: AssignmentFilter, query = ""): TenantAssignments[] {
  const q = query.trim().toLowerCase();
  return rows.filter((t) => {
    if (filter === "no_owner" && ofKind(t, "owner").length > 0) return false;
    if (filter === "pool" && !t.in_pool) return false;
    if (!q) return true;
    return [t.name, t.slug, ...t.assignments.map(staffLabel)].some((s) => (s ?? "").toLowerCase().includes(q));
  });
}

/** Staff that can still be added to a tenant (not assigned to it yet). */
export function addableStaff(staff: AssignableStaff[], t: Pick<TenantAssignments, "assignments">): AssignableStaff[] {
  const taken = new Set(t.assignments.map((a) => a.admin_user_id));
  return staff.filter((s) => !taken.has(s.admin_user_id));
}

/** The kinds a new assignment may take: owner only when the tenant has none (one owner per tenant, CIS 364). */
export function availableKinds(t: Pick<TenantAssignments, "assignments">): AssignmentKind[] {
  return ofKind(t, "owner").length > 0 ? ["backup", "support"] : [...ASSIGNMENT_KINDS];
}
