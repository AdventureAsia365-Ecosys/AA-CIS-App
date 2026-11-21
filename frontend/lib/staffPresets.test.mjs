// AA-1150 / AA-1155 (spec task T14, root spec R5.7): the Staff page's people-role presets are data (GET
// /api/admin/platform/presets); lib/staffPresets.ts plans them on an account like the backend
// (api/services/staff_presets.py plan(); tests/unit/test_aa1155_staff_presets.py). Invented preset codes and labels.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import {
  BOOKING_ROLES, PRESETS_PATH, RETIRED_BOOKING_ROLES, activePresets, createRoles, currentPreset, planChanges,
  presetBlockedWhy, presetInPlace, presetPlan,
} from "./staffPresets.ts";

const FRONTEND = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (p) => readFileSync(join(FRONTEND, p), "utf8");

const preset = (over) => ({
  code: "zz_preset", label: "Invented preset", description: "", cis_role: null, booking_mode: "keep",
  booking_role: null, crm_mode: "keep", crm_roles: [], additive_only: false, cis_keep_roles: [],
  booking_keep_roles: [], sort_order: 100, archived: false, ...over,
});

const LEAD = preset({ code: "zz_lead", label: "Zz lead", cis_role: "admin", booking_mode: "set", booking_role: "admin",
  crm_mode: "add", crm_roles: ["platform_admin"], sort_order: 10 });
const CHIEF = preset({ code: "zz_chief", label: "Zz chief", cis_role: "admin", booking_mode: "set", booking_role: "admin",
  sort_order: 20 });
const WRITER = preset({ code: "zz_writer", label: "Zz writer", cis_role: "content_lead", booking_mode: "clear",
  crm_mode: "replace", sort_order: 30 });
const DESK = preset({ code: "zz_desk", label: "Zz desk", cis_role: "customer_success", booking_mode: "set",
  booking_role: "contracting", additive_only: true, cis_keep_roles: ["admin", "content_lead", "customer_success"],
  booking_keep_roles: ["admin", "contracting"], sort_order: 40 });
const OLD = preset({ code: "zz_old", label: "Zz old", cis_role: "support", archived: true, sort_order: 1 });
const ALL = [DESK, OLD, WRITER, CHIEF, LEAD];

test("active presets: archived left out, by sort order", () => {
  assert.deepEqual(activePresets(ALL).map((p) => p.code), ["zz_lead", "zz_chief", "zz_writer", "zz_desk"]);
  assert.deepEqual(activePresets(undefined), []);
});

test("a plan sets, clears, replaces and adds like the backend", () => {
  assert.deepEqual(presetPlan(LEAD, { role: "content", booking_role: "sales", crm_roles: ["support"] }),
    { role: "admin", booking: "admin", crm: ["platform_admin", "support"], lost: [] });
  assert.deepEqual(presetPlan(CHIEF, { role: "content", booking_role: null, crm_roles: ["ops"] }),
    { role: "admin", booking: "admin", lost: [] });                 // CRM kept
  assert.deepEqual(presetPlan(WRITER, { role: "admin", booking_role: "contracting", crm_roles: ["ops"] }),
    { role: "content_lead", booking: null, crm: [], lost: [] });
});

test("an additive-only preset fills an empty slot, keeps a listed role and never replaces another one", () => {
  assert.deepEqual(presetPlan(DESK, { role: "viewer", booking_role: null }),
    { role: "customer_success", booking: "contracting", lost: [] });
  assert.deepEqual(presetPlan(DESK, { role: "content_lead", booking_role: "admin" }), { lost: [] });
  assert.deepEqual(presetPlan(DESK, { role: "customer_success", booking_role: "content_creator" }),
    { booking: "contracting", lost: [] });                          // a retired grant gives nothing
  assert.deepEqual(presetPlan(DESK, { role: "finance", booking_role: "sales" }).lost,
    [{ app: "cis", role: "finance" }, { app: "booking", role: "sales" }]);
  assert.deepEqual(presetPlan(DESK, { role: "custom_zz_role", booking_role: "custom_zz_ops" }).lost,
    [{ app: "cis", role: "custom_zz_role" }, { app: "booking", role: "custom_zz_ops" }]);
});

test("the current preset is the first active one that changes nothing", () => {
  assert.equal(currentPreset(ALL, { role: "admin", booking_role: "admin", crm_roles: ["platform_admin"] })?.code, "zz_lead");
  assert.equal(currentPreset(ALL, { role: "admin", booking_role: "admin", crm_roles: [] })?.code, "zz_chief");
  assert.equal(currentPreset(ALL, { role: "content_lead", booking_role: null, crm_roles: [] })?.code, "zz_writer");
  assert.equal(currentPreset(ALL, { role: "customer_success", booking_role: "contracting" })?.code, "zz_desk");
  assert.equal(currentPreset(ALL, { role: "support", booking_role: null })?.code, undefined);   // archived never
  assert.ok(!presetInPlace(WRITER, { role: "content_lead", booking_role: "contracting", crm_roles: [] }));
});

test("only the roles that change are listed in the confirm", () => {
  const held = { role: "content_lead", booking_role: "sales", crm_roles: [] };
  assert.deepEqual(planChanges(presetPlan(WRITER, held), held), { booking: { from: "sales", to: null } });
});

test("guards on a row: self-demote, would replace, CRM table missing", () => {
  const self = { role: "admin", booking_role: "admin", crm_roles: [] };
  assert.equal(presetBlockedWhy(WRITER, self, true, true), "ownRole");
  assert.equal(presetBlockedWhy(LEAD, self, true, true), null);                       // admin is never a demotion
  assert.equal(presetBlockedWhy(DESK, { role: "finance", booking_role: null }, false, true), "wouldReplace");
  assert.equal(presetBlockedWhy(LEAD, { role: "content", booking_role: null }, false, false), "crmUnavailable");
  assert.equal(presetBlockedWhy(CHIEF, { role: "content", booking_role: null }, false, false), null);
});

test("Add staff: a preset's roles on an empty account, or the custom fields", () => {
  assert.deepEqual(createRoles(LEAD, { role: "content", booking: "", crm: [] }),
    { role: "admin", booking: "admin", crm: ["platform_admin"] });
  assert.deepEqual(createRoles(WRITER, { role: "content", booking: "sales", crm: ["ops"] }),
    { role: "content_lead", booking: null, crm: [] });
  assert.deepEqual(createRoles(DESK, { role: "content", booking: "", crm: [] }),
    { role: "customer_success", booking: "contracting", crm: [] });
  assert.deepEqual(createRoles(preset({ crm_mode: "add", crm_roles: ["ops"] }), { role: "support", booking: "", crm: [] }),
    { role: "support", booking: null, crm: ["ops"] });                // a kept CIS role comes from the form
  assert.deepEqual(createRoles(null, { role: "support", booking: "", crm: ["support"] }),
    { role: "support", booking: null, crm: ["support"] });
});

test("R5.7: no preset literal is left in the frontend; the Staff page reads the presets from the API", () => {
  const lib = read("lib/staffPresets.ts");
  for (const code of ["platform_owner", "business_owner", "content_ops", "pricing_team"]) {
    assert.ok(!lib.includes(code), `staffPresets.ts names ${code}`);
    for (const f of ["app/admin/platform/staff/PeoplePresets.tsx", "app/admin/platform/staff/StaffManage.tsx"]) {
      assert.ok(!read(f).includes(`"${code}"`), `${f} names ${code}`);
    }
  }
  assert.ok(!/PEOPLE_PRESET|PRICING_/.test(lib));
  assert.equal(PRESETS_PATH, "/api/admin/platform/presets");
  assert.match(read("app/admin/platform/staff/PeoplePresets.tsx"), /apiGet<PresetList>\(PRESETS_PATH\)/);
});

test("content_creator is retired: never offered, still named on an old grant", () => {
  assert.ok(!BOOKING_ROLES.includes("content_creator"));
  assert.deepEqual([...RETIRED_BOOKING_ROLES], ["content_creator"]);
  const messages = read("app/admin/platform/staff/messages.ts");
  assert.match(messages, /contracting: "Product & operations"/);
  assert.match(messages, /content_creator: "Content creator \(retired\)"/);
});

test("the backend lists the same roles and guard sets", () => {
  const services = join(FRONTEND, "..", "api", "services");
  const directory = readFileSync(join(services, "staff_directory.py"), "utf8");
  const presets = readFileSync(join(services, "staff_presets.py"), "utf8");
  assert.match(directory, /BOOKING_ROLES: tuple\[str, \.\.\.\] = \("admin", "sales", "contracting", "finance"\)/);
  assert.match(presets, /RETIRED_BOOKING_ROLES: tuple\[str, \.\.\.\] = \("content_creator",\)/);
  assert.match(presets, /CIS_EMPTY_ROLES = frozenset\(\{"viewer"\}\)/);
  assert.match(presets, /BOOKING_MODES: tuple\[str, \.\.\.\] = \("keep", "set", "clear"\)/);
  assert.match(presets, /CRM_MODES: tuple\[str, \.\.\.\] = \("keep", "replace", "add"\)/);
});
