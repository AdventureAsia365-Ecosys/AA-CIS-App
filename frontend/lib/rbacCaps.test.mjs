// frontend/lib/rbacCaps.test.mjs
// AA-1155 spec task T10 (R5.2) — the admin pages and the BFF rules are decided by capabilities:
//   1. every PAGE_RULES / API_WRITE_RULES / API_READ_RULES / API_RULES entry's role list is exactly the built-in roles
//      holding one of its capabilities (nobody gained or lost a page when the rules moved to capabilities);
//   2. a verified session with a custom role sees exactly what its capability list opens (the CIS "Content" role:
//      content_lead minus content.settings.write, plus tenant.read), and a custom role without capabilities nothing;
//   3. a custom role string alone holds nothing until the admin shell registers its capabilities;
//   4. the landing page of a custom role follows its capabilities.
// Run: npm run test:unit.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  API_READ_RULES, API_RULES, API_WRITE_RULES, PAGE_RULES, ROLE_PERMS, STAFF_ROLES, can, canReadAdminPath, canSeePage,
  canWriteAdminPath, capabilitiesOf, isStaffSubject, landingPage, passesApiRule, registerSessionCapabilities,
} from "./rbac.ts";

const holders = (caps) =>
  STAFF_ROLES.filter((r) => caps === null || caps.some((c) => ROLE_PERMS[r].includes(c))).slice().sort();

test("1. every rule's roles are exactly the built-in roles holding one of its capabilities", () => {
  for (const [name, rules] of [["page", PAGE_RULES], ["write", API_WRITE_RULES], ["read", API_READ_RULES]]) {
    for (const rule of rules) {
      assert.ok(rule.caps.length > 0, `${name} ${rule.prefix}: no capability`);
      assert.deepEqual([...new Set(rule.roles)].sort(), holders(rule.caps), `${name} ${rule.prefix}`);
    }
  }
  for (const [key, rule] of Object.entries(API_RULES)) {
    assert.deepEqual([...rule.roles].sort(), holders(rule.caps), `api ${key}`);
  }
});

test("1b. a built-in role decides the same as before (role list = capability decision)", () => {
  for (const role of STAFF_ROLES) {
    for (const rule of PAGE_RULES) {
      assert.equal(canSeePage(role, rule.prefix), rule.roles.includes(role), `${role} ${rule.prefix}`);
      assert.equal(canSeePage({ role, capabilities: ["staff.read"] }, rule.prefix), rule.roles.includes(role),
        `${role} ${rule.prefix}: a built-in role ignores a session list`);
    }
  }
});

// The CIS "Content" custom role (CIS 462 seed): content_lead minus content.settings.write, plus tenant.read.
const CONTENT = [...ROLE_PERMS.content_lead.filter((c) => c !== "content.settings.write"), "tenant.read"];
const session = { role: "custom_content", capabilities: CONTENT };

test("2. a custom session sees what its capabilities open", () => {
  assert.ok(isStaffSubject(session));
  for (const page of ["/admin/my-work", "/admin/master-content", "/admin/review", "/admin/upload", "/admin/photos",
    "/admin/atom-curation", "/admin/tenant-qa", "/admin/lab", "/admin/docs", "/admin/tenants"]) {
    assert.ok(canSeePage(session, page), page);
  }
  for (const page of ["/admin/seo-intelligence", "/admin/platform", "/admin/platform/roles", "/admin/overview",
    "/admin/ai-gateway", "/admin/tenants/plans", "/admin/tenants/view-as", "/admin/jobs"]) {
    assert.ok(!canSeePage(session, page), page);
  }
  assert.ok(can(session, "content.publish") && can(session, "tenant.read"));
  assert.ok(!can(session, "content.settings.write") && !can(session, "staff.read"));
  assert.ok(canReadAdminPath(session, "docs/manifest"));
  assert.ok(!canReadAdminPath(session, "platform/staff") && !canReadAdminPath(session, "ai-gateway/providers"));
  assert.ok(canWriteAdminPath(session, "POST", "content-quality/pieces/x"));
  assert.ok(!canWriteAdminPath(session, "POST", "settings/registry/x/proposals"));
  assert.ok(passesApiRule(session, API_RULES.admin) && passesApiRule(session, API_RULES.pipeline));
  assert.ok(!passesApiRule(session, ["admin", "customer_success", "support"]), "a role list never admits a custom role");
});

test("2b. a custom session with no usable capability is no staff session", () => {
  const empty = { role: "custom_empty", capabilities: ["made.up"] };
  assert.equal(isStaffSubject(empty), false);
  assert.equal(passesApiRule(empty, API_RULES.admin), false);
  assert.equal(canSeePage(empty, "/admin/docs"), false);
  assert.equal(isStaffSubject({ role: "not_a_role", capabilities: CONTENT }), false);
});

test("3. a custom role string holds nothing until the shell registers its capabilities", () => {
  assert.deepEqual(capabilitiesOf("custom_registered"), []);
  assert.equal(can("custom_registered", "content.read"), false);
  registerSessionCapabilities("custom_registered", ["content.read", "made.up"]);
  assert.deepEqual([...capabilitiesOf("custom_registered")], ["content.read"]);
  assert.ok(canSeePage("custom_registered", "/admin/review"));
  registerSessionCapabilities("admin", ["staff.read"]);              // never for a built-in role
  assert.deepEqual(capabilitiesOf("admin"), ROLE_PERMS.admin);
});

test("4. the landing page of a custom role follows its capabilities", () => {
  assert.equal(landingPage(session), "/admin/my-work");
  assert.equal(landingPage({ role: "custom_desk", capabilities: ["tenant.book.read", "notifications.read"] }),
    "/admin/tenants");
  assert.equal(landingPage({ role: "custom_spend", capabilities: ["spend.read"] }), "/admin/ai-gateway");
  assert.equal(landingPage({ role: "custom_docs", capabilities: ["docs.read"] }), "/admin/docs");
  for (const role of STAFF_ROLES) assert.ok(canSeePage(role, landingPage(role)), role);
  assert.ok(canSeePage(session, landingPage(session)));
});
