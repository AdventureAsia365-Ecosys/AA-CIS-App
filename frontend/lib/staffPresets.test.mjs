// AA-1150 (lean staff roles step 1): the Staff page's three people-role presets and the retired Booking role, as the
// backend applies them (api/services/staff_directory.py change_roles; tests/unit/test_aa1150_lean_presets.py).
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import {
  BOOKING_ROLES, PEOPLE_PRESETS, PEOPLE_PRESET_ROLES, RETIRED_BOOKING_ROLES, createRoles, peoplePresetOf,
} from "./staffPresets.ts";

const FRONTEND = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (p) => readFileSync(join(FRONTEND, p), "utf8");

test("three presets in order, Administrator first", () => {
  assert.deepEqual([...PEOPLE_PRESETS], ["platform_owner", "business_owner", "content_ops"]);
});

test("Content + Product & operations is CIS content_lead with no Booking role and no CRM role", () => {
  assert.deepEqual(PEOPLE_PRESET_ROLES.content_ops, { role: "content_lead", booking: null, crm: [] });
  assert.deepEqual(createRoles("content_ops", { role: "content", booking: "sales", crm: ["ops"] }),
    { role: "content_lead", booking: null, crm: [] });
});

test("Administrator and Business owner give CIS admin + Booking admin; only Administrator adds CRM Agency admin", () => {
  assert.deepEqual(createRoles("platform_owner", { role: "content", booking: "", crm: [] }),
    { role: "admin", booking: "admin", crm: ["platform_admin"] });
  assert.deepEqual(createRoles("business_owner", { role: "content", booking: "", crm: [] }),
    { role: "admin", booking: "admin", crm: [] });
  assert.deepEqual(createRoles("custom", { role: "support", booking: "", crm: ["support"] }),
    { role: "support", booking: null, crm: ["support"] });
});

test("the current preset of an account", () => {
  assert.equal(peoplePresetOf({ role: "admin", booking_role: "admin", crm_roles: ["platform_admin"] }), "platform_owner");
  assert.equal(peoplePresetOf({ role: "admin", booking_role: "admin", crm_roles: [] }), "business_owner");
  assert.equal(peoplePresetOf({ role: "content_lead", booking_role: null, crm_roles: [] }), "content_ops");
  assert.equal(peoplePresetOf({ role: "content_lead", booking_role: "contracting", crm_roles: [] }), null);
  assert.equal(peoplePresetOf({ role: "customer_success", booking_role: "contracting" }), null);   // Pricing team
});

test("content_creator is retired: never offered, still named on an old grant", () => {
  assert.ok(!BOOKING_ROLES.includes("content_creator"));
  assert.deepEqual([...RETIRED_BOOKING_ROLES], ["content_creator"]);
  const messages = read("app/admin/platform/staff/messages.ts");
  assert.match(messages, /contracting: "Product & operations"/);
  assert.match(messages, /content_creator: "Content creator \(retired\)"/);
});

test("the backend lists the same roles and presets", () => {
  const services = join(FRONTEND, "..", "api", "services");
  const directory = readFileSync(join(services, "staff_directory.py"), "utf8");
  const presets = readFileSync(join(services, "staff_presets.py"), "utf8");
  assert.match(directory, /BOOKING_ROLES: tuple\[str, \.\.\.\] = \("admin", "sales", "contracting", "finance"\)/);
  assert.match(presets, /RETIRED_BOOKING_ROLES: tuple\[str, \.\.\.\] = \("content_creator",\)/);
  for (const p of PEOPLE_PRESETS) assert.ok(presets.includes(`"${p}"`), p);
  assert.match(presets, /CONTENT_OPS_CIS_ROLE = "content_lead"/);
});
