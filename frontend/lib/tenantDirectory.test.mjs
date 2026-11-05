// frontend/lib/tenantDirectory.test.mjs
// AA-1035 (directory capability UI): the Tenants Directory shows only the write controls the backend allows the role
// (lib/tenantDirectory.ts, a mirror of api/core/staff_authz.py and the field-level PATCH rule). Run: npm run test:unit.

import assert from "node:assert/strict";
import { test } from "node:test";

import { STAFF_ROLES, can } from "./rbac.ts";
import { canPatchBillingFields, tenantDirectoryControls } from "./tenantDirectory.ts";

const NONE = { create: false, offboardRestore: false, statusToggle: false, unmappedMarkets: false };

test("admin keeps every Directory control", () => {
  assert.deepEqual(tenantDirectoryControls("admin"),
    { create: true, offboardRestore: true, statusToggle: true, unmappedMarkets: true });
});

test("finance toggles suspend / resume only (billing.write); no create, offboard, restore or market banner", () => {
  assert.deepEqual(tenantDirectoryControls("finance"), { ...NONE, statusToggle: true });
});

test("customer_success, support and viewer get no write control and no unmapped-markets call", () => {
  for (const role of ["customer_success", "support", "viewer"]) {
    assert.deepEqual(tenantDirectoryControls(role), NONE, role);
  }
});

test("an unknown, empty or missing role (before the identity cookie is read) gets none", () => {
  for (const role of ["", null, undefined, "tenant", "reviewer"]) {
    assert.deepEqual(tenantDirectoryControls(role), NONE, String(role));
  }
});

test("every role: the flags follow the capability map (create = offboard = tenant.write; banner = tenant.read)", () => {
  for (const role of STAFF_ROLES) {
    const c = tenantDirectoryControls(role);
    assert.equal(c.create, can(role, "tenant.write"), role);
    assert.equal(c.offboardRestore, can(role, "tenant.write"), role);
    assert.equal(c.statusToggle, can(role, "tenant.write") || can(role, "billing.write"), role);
    assert.equal(c.unmappedMarkets, can(role, "tenant.read"), role);
  }
});

test("the Tenant 360 plan editor follows the same billing-field rule: admin and finance, never customer_success", () => {
  assert.equal(canPatchBillingFields("admin"), true);
  assert.equal(canPatchBillingFields("finance"), true);
  for (const role of ["customer_success", "support", "viewer", "content", ""]) {
    assert.equal(canPatchBillingFields(role), false, role);
  }
});
