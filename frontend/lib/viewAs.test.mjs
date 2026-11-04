// frontend/lib/viewAs.test.mjs — AA-1035 R2 slice 2: the pure parts of the read-only "view as partner"
// (lib/viewAs.ts), the Assignments page (lib/tenantAssignments.ts) and their page rules. Run: npm run test:unit.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  VIEW_AS_BFF_HEADER, VIEW_AS_COOKIE_PATH, VIEW_AS_READ_ROUTES, VIEW_AS_ROLES, VIEW_AS_ROUTE_ROLES, cookieMaxAge,
  formatCountdown, isViewAsStartPath, pickText, rowsOf, secondsLeft, startProblem, viewAsClaims, viewAsPathAllowed,
  viewAsReadUrl, viewAsReadsForRole, viewAsRoleMayRead,
} from "./viewAs.ts";
import {
  addableStaff, assignmentPath, availableKinds, filterTenants, ofKind, staffLabel,
} from "./tenantAssignments.ts";
import { ROLE_PERMS, canSeePage, can, canWriteAdminPath } from "./rbac.ts";

const b64 = (o) => Buffer.from(JSON.stringify(o)).toString("base64url");
const jwtOf = (payload) => `${b64({ alg: "HS256", typ: "JWT" })}.${b64(payload)}.signature`;

test("the BFF forwards only the allowed reads, matched in full", () => {
  for (const ok of ["/v1/quota", "/v1/tours/my-versions", "/v1/tours/versions/abc", "/v1/account/members"]) {
    assert.ok(viewAsPathAllowed(ok, "admin"), ok);
  }
  for (const no of ["/v1/slate", "/v1/quota/", "/v1/quotas", "/v1/tours/versions/abc/export-docx",
    "/v1/account/activity/export", "/v1/integrations/wordpress", "/admin/tenants", "/v1/tours/versions/",
    "/v1/tours/versions/a/b", "/v1/quota?x=1", "/v1/billing"]) {
    assert.ok(!viewAsPathAllowed(no, "admin"), no);
  }
  assert.equal(VIEW_AS_READ_ROUTES.length, 12);
  assert.ok(!VIEW_AS_READ_ROUTES.includes("/v1/billing"));
  assert.equal(viewAsReadUrl("/v1/quota"), "/api/view-as/v1/quota");
  assert.equal(VIEW_AS_COOKIE_PATH, "/api/view-as");
});

test("the reads depend on the role: view-as never widens a role's own reads (S268 fix pass)", () => {
  const all = [...VIEW_AS_READ_ROUTES];
  assert.deepEqual(viewAsReadsForRole("admin"), all);
  assert.deepEqual(viewAsReadsForRole("customer_success"), all);
  assert.deepEqual(viewAsReadsForRole("support"), all.filter((t) => t !== "/v1/quota"));
  for (const role of ["finance", "content", "content_lead", "tenant_qa", "ai_ops", "viewer", null, undefined]) {
    assert.deepEqual(viewAsReadsForRole(role), [], String(role));
  }
  assert.ok(!viewAsPathAllowed("/v1/quota", "support"));
  assert.ok(viewAsPathAllowed("/v1/marketplace", "support"));
  assert.ok(viewAsPathAllowed("/v1/quota", "customer_success"));
  assert.ok(!viewAsPathAllowed("/v1/marketplace", "finance"));
  assert.ok(!viewAsPathAllowed("/v1/marketplace", undefined));
  assert.ok(!viewAsRoleMayRead("admin", "/v1/billing"));
  for (const [template, roles] of Object.entries(VIEW_AS_ROUTE_ROLES)) {
    assert.ok(VIEW_AS_READ_ROUTES.includes(template), template);
    for (const r of roles) assert.ok(VIEW_AS_ROLES.includes(r), r);
  }
});

test("the generic admin proxy may never start a view-as session (the token stays out of the page)", () => {
  const id = "22222222-2222-4222-8222-222222222222";
  for (const p of [`tenants/${id}/view-as`, `tenants/${id}/view-as/`, `Tenants/${id}/VIEW-AS`, `tenants/x/view-as/extra`,
    `/tenants/${id}/view-as`]) {
    assert.ok(isViewAsStartPath(p), p);
  }
  for (const p of ["tenants", `tenants/${id}`, `tenants/${id}/assignments`, "view-as/sessions", `view-as/sessions/${id}/end`,
    `tenants/${id}/view-as-x`, "tenants/view-as"]) {
    assert.ok(!isViewAsStartPath(p), p);
  }
  // the write rule alone would let the tenant desk through: the explicit refusal is what keeps the token out
  assert.ok(canWriteAdminPath("support", "POST", `tenants/${id}/view-as`));
  assert.equal(VIEW_AS_BFF_HEADER, "x-view-as-client");
});

test("the generic admin proxy source refuses the start path before forwarding", async () => {
  const { readFile } = await import("node:fs/promises");
  const src = await readFile(new URL("../app/api/admin/[...path]/route.ts", import.meta.url), "utf8");
  const guard = src.indexOf("isViewAsStartPath(pathStr)");
  assert.ok(guard > 0, "the generic proxy must call isViewAsStartPath");
  assert.ok(guard < src.indexOf("await fetch(url"), "the refusal must come before the upstream call");
  const bff = await readFile(new URL("../app/api/view-as/route.ts", import.meta.url), "utf8");
  assert.ok(bff.includes('[VIEW_AS_BFF_HEADER]: "bff"'), "the view-as BFF sets the header on the start call");
  assert.ok(!src.includes("VIEW_AS_BFF_HEADER"), "the generic proxy never sets the BFF header");
});

test("view-as roles are exactly the roles holding tenant.view_as", () => {
  const holders = Object.entries(ROLE_PERMS).filter(([, caps]) => caps.includes("tenant.view_as")).map(([r]) => r);
  assert.deepEqual([...holders].sort(), [...VIEW_AS_ROLES].sort());
  assert.ok(!can("finance", "tenant.view_as") && !can("content", "tenant.view_as"));
});

test("the token claims are read without trusting anything else", () => {
  const t = jwtOf({ vas: "s-1", stf: "u-1", exp: 2_000_000_000, sub: "t-1" });
  assert.deepEqual(viewAsClaims(t), { sessionId: "s-1", staffId: "u-1", exp: 2_000_000_000 });
  assert.equal(viewAsClaims(jwtOf({ stf: "u-1", exp: 1 })), null);         // not a view-as token
  assert.equal(viewAsClaims(jwtOf({ vas: 1, stf: "u-1", exp: 1 })), null);
  assert.equal(viewAsClaims("not-a-jwt"), null);
  assert.equal(viewAsClaims(undefined), null);
});

test("countdown, seconds left and the cookie lifetime", () => {
  const now = Date.parse("2026-11-04T10:00:00Z");
  assert.equal(secondsLeft("2026-11-04T10:30:00Z", now), 1800);
  assert.equal(secondsLeft("2026-11-04T09:00:00Z", now), 0);
  assert.equal(secondsLeft(null, now), 0);
  assert.equal(secondsLeft("bad", now), 0);
  assert.equal(formatCountdown(1800), "30:00");
  assert.equal(formatCountdown(65), "01:05");
  assert.equal(formatCountdown(3725), "1:02:05");
  assert.equal(formatCountdown(-4), "00:00");
  assert.equal(cookieMaxAge("2026-11-04T10:30:00Z", now), 1800);
  assert.equal(cookieMaxAge("2026-11-04T13:00:00Z", now), 3600);           // never beyond 60 minutes
  assert.equal(cookieMaxAge("2026-11-04T09:00:00Z", now), 1);
});

test("the start dialog checks what the API checks", () => {
  assert.equal(startProblem("  ", "", 30), "reason");
  assert.equal(startProblem("ok", "", 30), "reason");
  assert.equal(startProblem("x".repeat(501), "", 30), "reason");
  assert.equal(startProblem("Checking a held piece", "x".repeat(121), 30), "ticket");
  assert.equal(startProblem("Checking a held piece", "", 61), "minutes");
  assert.equal(startProblem("Checking a held piece", "", 4), "minutes");
  assert.equal(startProblem("Checking a held piece", "SUP-1", 60), null);
});

test("list answers of the tenant reads", () => {
  assert.deepEqual(rowsOf({ data: [{ a: 1 }] }), [{ a: 1 }]);
  assert.deepEqual(rowsOf({ items: [{ b: 2 }], unread: 1 }), [{ b: 2 }]);
  assert.deepEqual(rowsOf({ tours: [{ c: 3 }] }), [{ c: 3 }]);
  assert.deepEqual(rowsOf([{ d: 4 }]), [{ d: 4 }]);
  assert.deepEqual(rowsOf(null), []);
  assert.equal(pickText({ title: "", tour_name: "Misty Ridge Walk" }, ["title", "tour_name"]), "Misty Ridge Walk");
  assert.equal(pickText({ n: 0 }, ["n"]), "0");
  assert.equal(pickText({ x: { y: 1 } }, ["x"]), null);
});

test("page rules: Assignments and View as for the tenant book roles only", () => {
  for (const page of ["/admin/tenants/assignments", "/admin/tenants/view-as"]) {
    for (const role of ["admin", "customer_success", "support"]) assert.ok(canSeePage(role, page), `${role} ${page}`);
    for (const role of ["finance", "content", "content_lead", "tenant_qa", "ai_ops", "viewer", "reviewer"]) {
      assert.ok(!canSeePage(role, page), `${role} ${page}`);
    }
  }
  assert.ok(canSeePage("finance", "/admin/tenants"));   // the directory rule is unchanged
});

const T = {
  tenant_id: "t-1", name: "Saffron Lane Travel", slug: "saffron-lane", kind: "agency", segment: null,
  status: "active", in_pool: false,
  assignments: [
    { admin_user_id: "u-owner-0001", kind: "owner", display_name: "Wren Example", staff_role: "customer_success",
      is_active: true, created_at: null, created_by: "admin:x" },
    { admin_user_id: "u-supp-00002", kind: "support", display_name: "", staff_role: "support", is_active: true,
      created_at: null, created_by: "admin:x" },
  ],
};
const POOL = { ...T, tenant_id: "t-2", name: "Harbor Kite Tours", slug: "harbor-kite", in_pool: true, assignments: [] };

test("assignments: kinds, labels, filters, who can still be added", () => {
  assert.deepEqual(ofKind(T, "owner").map((a) => a.admin_user_id), ["u-owner-0001"]);
  assert.equal(staffLabel(T.assignments[1]), "u-supp-0");
  assert.deepEqual(availableKinds(T), ["backup", "support"]);                // one owner per tenant
  assert.deepEqual(availableKinds(POOL), ["owner", "backup", "support"]);
  assert.deepEqual(filterTenants([T, POOL], "no_owner").map((t) => t.tenant_id), ["t-2"]);
  assert.deepEqual(filterTenants([T, POOL], "pool").map((t) => t.tenant_id), ["t-2"]);
  assert.deepEqual(filterTenants([T, POOL], "all", "wren").map((t) => t.tenant_id), ["t-1"]);
  assert.deepEqual(filterTenants([T, POOL], "all", "KITE").map((t) => t.tenant_id), ["t-2"]);
  const staff = [{ admin_user_id: "u-owner-0001", display_name: "Wren Example", email: null, role: "customer_success" },
    { admin_user_id: "u-new-000003", display_name: "Moss Example", email: null, role: "support" }];
  assert.deepEqual(addableStaff(staff, T).map((s) => s.admin_user_id), ["u-new-000003"]);
  assert.equal(assignmentPath("t 1", "u/1"), "/api/admin/tenants/t%201/assignments/u%2F1");
});
