// frontend/lib/tenantDirectory.ts
// AA-1035 (directory capability UI): which write controls the Tenants Directory (app/admin/tenants/page.tsx) and the
// Tenant 360 plan editor show for a staff role. A mirror of api/core/staff_authz.py POLICY plus the field-level PATCH
// rule (api/routers/admin.py _require_tenant_patch, BILLING_PATCH_FIELDS = plan_tier, is_active):
//   - POST /admin/tenants, /offboard, /restore: tenant.write (admin);
//   - PATCH /admin/tenants/{id} with plan_tier or is_active: tenant.write or billing.write (admin, finance);
//     customer_success's tenant.book.write excludes both fields;
//   - GET /admin/unmapped-market-requests: tenant.read (admin).
// UI only: the backend stays the authority and re-checks every call. Dependency-free apart from rbac.ts, so node --test
// loads it (lib/tenantDirectory.test.mjs).

import { can } from "./rbac.ts";

export interface TenantDirectoryControls {
  /** "New tenant" in the page header and its create dialog. */
  create: boolean;
  /** Offboard / Restore in each row (and their dialogs); Detail is shown to everyone. */
  offboardRestore: boolean;
  /** The status pill toggles active <-> suspended; otherwise it is read-only text. */
  statusToggle: boolean;
  /** The "tenants waiting on a market" banner and its GET /admin/unmapped-market-requests call. */
  unmappedMarkets: boolean;
}

/** May `role` send the billing fields of PATCH /admin/tenants/{id} (plan_tier, is_active)? */
export function canPatchBillingFields(role: string | null | undefined): boolean {
  return can(role, "tenant.write") || can(role, "billing.write");
}

/** The Directory's write controls for `role` (an unknown or empty role gets none of them). */
export function tenantDirectoryControls(role: string | null | undefined): TenantDirectoryControls {
  return {
    create: can(role, "tenant.write"),
    offboardRestore: can(role, "tenant.write"),
    statusToggle: canPatchBillingFields(role),
    unmappedMarkets: can(role, "tenant.read"),
  };
}
