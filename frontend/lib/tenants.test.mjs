// frontend/lib/tenants.test.mjs
// AA-919: the pure parts of the shared admin tenant picker (lib/tenants.ts) and the Releases tab owner label
// (lib/writingMemory.ts owner). Run: npm run test:unit.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  TENANTS_API, TENANTS_QUERY_KEY, filterTenants, shortId, tenantLabel, tenantOptionText,
} from "./tenants.ts";
import { owner } from "./writingMemory.ts";

const A = { tenant_id: "aaaaaaaa-1111-4111-8111-111111111111", name: "Zeta Travel", slug: "zeta", status: "active" };
const B = { tenant_id: "bbbbbbbb-2222-4222-8222-222222222222", name: "", slug: "alpha-tours", status: "offboarded" };
const C = { tenant_id: "cccccccc-3333-4333-8333-333333333333", name: "Beta Trips", slug: "Beta Trips" };
const byId = new Map([A, B, C].map((t) => [t.tenant_id, t]));

test("one tenant list for every page: the include-inactive list and the data-erasure cache key", () => {
  assert.equal(TENANTS_API, "/api/admin/tenants?include_inactive=true");
  assert.deepEqual([...TENANTS_QUERY_KEY], ["admin-tenants", "include-inactive"]);
});

test("tenantLabel: name, else slug, else the short id", () => {
  assert.equal(tenantLabel(A.tenant_id, byId), "Zeta Travel");
  assert.equal(tenantLabel(B.tenant_id, byId), "alpha-tours");
  assert.equal(tenantLabel("dddddddd-4444-4444-8444-444444444444", byId), "dddddddd");
  assert.equal(tenantLabel("dddddddd-4444-4444-8444-444444444444", undefined), "dddddddd");
  assert.equal(tenantLabel(null, byId), "—");
  assert.equal(shortId(undefined), "—");
});

test("tenantOptionText: name (slug), the status only when not active, no repeated slug", () => {
  assert.equal(tenantOptionText(A), "Zeta Travel (zeta)");
  assert.equal(tenantOptionText(B), "alpha-tours · offboarded");
  assert.equal(tenantOptionText(C), "Beta Trips");
});

test("filterTenants: case-insensitive on name, slug and id, sorted by name", () => {
  assert.deepEqual(filterTenants([A, B, C], "").map((t) => t.slug), ["alpha-tours", "Beta Trips", "zeta"]);
  assert.deepEqual(filterTenants([A, B, C], "ZET").map((t) => t.slug), ["zeta"]);
  assert.deepEqual(filterTenants([A, B, C], "cccccccc").map((t) => t.slug), ["Beta Trips"]);
  assert.deepEqual(filterTenants([A, B, C], "  nothing  "), []);
});

test("owner: Platform, the tenant name when given, else Tenant + short id", () => {
  assert.equal(owner("platform", null, "ignored"), "Platform");
  assert.equal(owner("tenant", A.tenant_id, "Zeta Travel"), "Zeta Travel");
  assert.equal(owner("tenant", A.tenant_id), "Tenant aaaaaaaa");
  assert.equal(owner("tenant", A.tenant_id, ""), "Tenant aaaaaaaa");
});
