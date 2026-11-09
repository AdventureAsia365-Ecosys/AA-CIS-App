// frontend/lib/viewAs.test.mjs — AA-1035 R2 slice 2: the pure parts of the read-only "view as partner"
// (lib/viewAs.ts), the Assignments page (lib/tenantAssignments.ts) and their page rules. Run: npm run test:unit.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  VIEW_AS_BFF_HEADER, VIEW_AS_COOKIE_PATH, VIEW_AS_READ_ROUTES, VIEW_AS_ROLES, VIEW_AS_ROLE_HEADER, VIEW_AS_ROUTE_ROLES,
  cookieMaxAge, formatCountdown, isViewAsStartPath, ownOpenSessionIds, ownViewAsClaims, pickText, rowsOf, secondsLeft,
  startProblem, viewAsClaims, viewAsForward, viewAsPathAllowed,
  viewAsReadUrl, viewAsReadsForRole, viewAsRoleMayRead,
} from "./viewAs.ts";
import {
  addableStaff, assignmentPath, availableKinds, filterTenants, ofKind, staffLabel,
} from "./tenantAssignments.ts";
import { ROLE_PERMS, canSeePage, can, canWriteAdminPath } from "./rbac.ts";

const b64 = (o) => Buffer.from(JSON.stringify(o)).toString("base64url");
const jwtOf = (payload) => `${b64({ alg: "HS256", typ: "JWT" })}.${b64(payload)}.signature`;

test("the BFF forwards only the allowed reads, matched in full", () => {
  for (const ok of ["/v1/quota", "/v1/tours/my-versions", "/v1/tours/versions/abc", "/v1/account/members",
    "/v1/account/add-ons"]) {
    assert.ok(viewAsPathAllowed(ok, "admin"), ok);
  }
  for (const no of ["/v1/slate", "/v1/quota/", "/v1/quotas", "/v1/tours/versions/abc/export-docx",
    "/v1/account/activity/export", "/v1/integrations/wordpress", "/admin/tenants", "/v1/tours/versions/",
    "/v1/tours/versions/a/b", "/v1/quota?x=1", "/v1/billing", "/v1/account/add-on-requests", "/v1/account/add-ons/x"]) {
    assert.ok(!viewAsPathAllowed(no, "admin"), no);
  }
  assert.equal(VIEW_AS_READ_ROUTES.length, 13);
  assert.ok(!VIEW_AS_READ_ROUTES.includes("/v1/billing"));
  assert.equal(viewAsReadUrl("/v1/quota"), "/api/view-as/v1/quota");
  assert.equal(VIEW_AS_COOKIE_PATH, "/api/view-as");
});

test("the reads depend on the role: view-as never widens a role's own reads (S268 fix pass)", () => {
  const all = [...VIEW_AS_READ_ROUTES];
  assert.deepEqual(viewAsReadsForRole("admin"), all);
  assert.deepEqual(viewAsReadsForRole("customer_success"), all);
  // AA-1036 review: support never reads the add-ons (subscription statuses, staff decision notes) either
  assert.deepEqual(viewAsReadsForRole("support"), all.filter((t) => t !== "/v1/quota" && t !== "/v1/account/add-ons"));
  assert.ok(!viewAsPathAllowed("/v1/account/add-ons", "support"));
  assert.ok(viewAsPathAllowed("/v1/account/add-ons", "customer_success"));
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
  const t = jwtOf({ vas: "s-1", stf: "u-1", srl: "support", exp: 2_000_000_000, sub: "t-1" });
  assert.deepEqual(viewAsClaims(t), { sessionId: "s-1", staffId: "u-1", startRole: "support", exp: 2_000_000_000 });
  assert.equal(viewAsClaims(jwtOf({ stf: "u-1", srl: "admin", exp: 1 })), null);   // not a view-as token
  assert.equal(viewAsClaims(jwtOf({ vas: 1, stf: "u-1", srl: "admin", exp: 1 })), null);
  assert.equal(viewAsClaims(jwtOf({ vas: "s-1", stf: "u-1", exp: 1 })), null);     // no start role: no session
  assert.equal(viewAsClaims("not-a-jwt"), null);
  assert.equal(viewAsClaims(undefined), null);
});

test("a role changed since the start is forwarded so the API ends the session; same role: the role's reads", () => {
  assert.equal(VIEW_AS_ROLE_HEADER, "x-view-as-staff-role");
  // same role: exactly viewAsPathAllowed
  assert.ok(viewAsForward("/v1/account/members", "support", "support"));
  assert.ok(!viewAsForward("/v1/quota", "support", "support"));
  assert.ok(viewAsForward("/v1/quota", "customer_success", "customer_success"));
  // role changed: any allowed read path goes to the API (with the role header), which ends and logs the session
  assert.ok(viewAsForward("/v1/quota", "support", "customer_success"));
  assert.ok(viewAsForward("/v1/account/members", "customer_success", "support"));
  // never a path outside the allowed reads, whatever the roles
  for (const no of ["/v1/slate", "/v1/billing", "/admin/tenants", "/v1/quota/"]) {
    assert.ok(!viewAsForward(no, "support", "customer_success"), no);
    assert.ok(!viewAsForward(no, "admin", "admin"), no);
  }
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

// ── S268 follow-ups: the session owner on every BFF path, the sign-out ends the member's own sessions ──────────────

const SID = "0b5b6a52-3c1f-4d8e-9a77-2f1e0c9d8b11";
const OTHER_SID = "6c2d9e10-7a4b-4f3e-8d21-5b9c0a7e6f33";
const ME = "9a1f3c22-5d6e-4b7a-8c9d-0e1f2a3b4c5d";
const SOMEONE = "1e2d3c4b-5a69-4788-97a6-b5c4d3e2f1a0";

test("the BFF uses a view-as cookie only for the member it was started by", () => {
  const mine = jwtOf({ vas: SID, stf: ME, srl: "support", exp: 2_000_000_000 });
  assert.deepEqual(ownViewAsClaims(mine, ME), { sessionId: SID, staffId: ME, startRole: "support", exp: 2_000_000_000 });
  assert.equal(ownViewAsClaims(mine, SOMEONE), null);                                     // a shared browser
  assert.equal(ownViewAsClaims(mine, ""), null);
  assert.equal(ownViewAsClaims(mine, undefined), null);
  assert.equal(ownViewAsClaims(jwtOf({ vas: "not-a-uuid", stf: ME, srl: "admin", exp: 1 }), ME), null);
  assert.equal(ownViewAsClaims(undefined, ME), null);
});

test("DELETE ends only the member's own session: the owner check comes before the end call", async () => {
  const { readFile } = await import("node:fs/promises");
  const src = await readFile(new URL("../app/api/view-as/route.ts", import.meta.url), "utf8");
  const del = src.slice(src.indexOf("export async function DELETE"));
  const check = del.indexOf("ownViewAsClaims(req.cookies.get(VIEW_AS_COOKIE)?.value, auth.adminId)");
  assert.ok(check > 0, "DELETE must read the cookie through ownViewAsClaims with the signed-in member's id");
  assert.ok(check < del.indexOf("/end`"), "the owner check must come before the end call");
  const get = src.slice(src.indexOf("export async function GET"), src.indexOf("export async function DELETE"));
  assert.ok(get.includes("ownViewAsClaims(cookie, auth.adminId)"), "GET keeps the same owner rule");
});

test("the sign-out keeps only the member's own open sessions of the list", () => {
  const answer = { scope: "all", sessions: [
    { session_id: SID, admin_user_id: ME, open: true },
    { session_id: OTHER_SID, admin_user_id: SOMEONE, open: true },          // an admin's list: another member's
    { session_id: OTHER_SID, admin_user_id: ME, open: false },              // already ended
    { session_id: "x", admin_user_id: ME, open: true },                     // not a session id
  ] };
  assert.deepEqual(ownOpenSessionIds(answer, ME), [SID]);
  assert.deepEqual(ownOpenSessionIds(answer, ""), []);
  assert.deepEqual(ownOpenSessionIds({ sessions: "no" }, ME), []);
  assert.deepEqual(ownOpenSessionIds(null, ME), []);
});

test("the staff sign-out ends the own view-as sessions before the revoke and clears the view-as cookie", async () => {
  const { readFile } = await import("node:fs/promises");
  const src = await readFile(new URL("../app/api/auth/admin-logout/route.ts", import.meta.url), "utf8");
  const post = src.slice(src.indexOf("export async function POST"));
  const end = post.indexOf("await endOwnViewAsSessions(request)");
  assert.ok(end > 0, "the sign-out must end the member's view-as sessions");
  assert.ok(end > post.indexOf("isSameOrigin(request"), "a cross-site sign-out changes nothing");
  assert.ok(end < post.indexOf("revokeRefreshToken("), "before the revoke (it invalidates the access token)");
  assert.ok(src.includes("ownOpenSessionIds(await res.json(), auth.adminId)"), "only the member's own sessions");
  assert.ok(src.includes("/admin/view-as/sessions?open_only=true"));
  assert.match(post, /cookies\.set\(VIEW_AS_COOKIE, "", \{[^}]*path: VIEW_AS_COOKIE_PATH, maxAge: 0/s);
  assert.ok(src.includes("AbortSignal.timeout(VIEW_AS_END_TIMEOUT_MS)"), "a slow API never holds the sign-out");
});
