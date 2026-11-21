// frontend/lib/rolesMatrix.test.mjs
// AA-1155 spec task T10 (R5.4) — Platform › Roles & permissions: the rows of the read-only page.
//   1. CIS and Booking roles (built-in and custom), the three CRM staff groups as static rows, TripPlanner as "no staff
//      roles", in that order;
//   2. no agent rows, no E2E rows, no person names, no issue ids (a row named after a test or an agent is dropped, an
//      issue id is cut from a label);
//   3. the page module names no person, no E2E address and no issue id in its UI text;
//   4. the helpers: capability columns, areas, the filter.
// Invented role names only. Run: npm run test:unit.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import {
  capabilityArea, cisCapabilityColumns, matchesFilter, matrixRows, rowsForApp, visibleRows,
} from "./rolesMatrix.ts";

const DATA = {
  cis: {
    builtin: [
      { code: "admin", label: "Administrator", capabilities: ["staff.read", "content.read"], assignable: true },
      { code: "viewer", label: "Viewer (read-only)", capabilities: ["content.read"], assignable: false },
    ],
    custom: [
      { code: "custom_desk", label: "Desk crew", based_on: "content", archived: false,
        capabilities: ["tenant.read", "content.read"] },
      { code: "custom_e2e_probe", label: "E2E probe", based_on: "content", archived: false, capabilities: ["x.y"] },
      { code: "custom_bot", label: "Agent helper", based_on: "content", archived: false, capabilities: ["x.y"] },
      { code: "custom_old", label: "Old desk AA-1234", based_on: "content", archived: true, capabilities: [] },
    ],
    custom_available: true,
  },
  booking: {
    builtin: [{ code: "admin", label: "Administrator" }, { code: "contracting", label: "Product & operations" }],
    custom: [{ code: "custom_ops_books", label: "Ops and books", based_on: "contracting", archived: false,
      capabilities: ["supply.read", "bill.write"] }],
    custom_available: true,
  },
  crm: { groups: [{ code: "platform_admin", label: "Agency admin" }, { code: "ops", label: "Operations" },
    { code: "support", label: "Support" }] },
  tripplanner: { staff_roles: false },
};

test("1. every app, in order, built-in before custom; the CRM groups static; TripPlanner without staff roles", () => {
  const rows = matrixRows(DATA);
  assert.deepEqual([...new Set(rows.map((r) => r.app))], ["cis", "booking", "crm", "tripplanner"]);
  assert.deepEqual(rowsForApp(rows, "cis").map((r) => `${r.kind}:${r.code}`),
    ["builtin:admin", "builtin:viewer", "custom:custom_desk", "custom:custom_old"]);
  assert.deepEqual(rowsForApp(rows, "booking").map((r) => [r.kind, r.capabilities]),
    [["builtin", null], ["builtin", null], ["custom", ["bill.write", "supply.read"]]]);
  assert.deepEqual(rowsForApp(rows, "crm").map((r) => r.kind), ["group", "group", "group"]);
  assert.deepEqual(rowsForApp(rows, "tripplanner").map((r) => r.kind), ["none"]);
  assert.equal(rowsForApp(rows, "cis").find((r) => r.code === "custom_old").archived, true);
});

test("2. no agent or E2E rows, no issue ids", () => {
  const rows = matrixRows(DATA);
  const text = JSON.stringify(rows);
  assert.ok(!/\be2e\b/i.test(text), "no E2E row");
  assert.ok(!/\bagent\b/i.test(text), "no agent row");
  assert.ok(!/AA-\d+/.test(text), "no issue id");
  assert.equal(rows.find((r) => r.code === "custom_old").label, "Old desk");
  assert.deepEqual(visibleRows([{ id: "x", app: "cis", kind: "custom", code: "custom_e2e", label: "Anything",
    capabilities: [], basedOn: null, archived: false }]), []);
});

test("3. the page's UI text names no person, no E2E identity and no issue id", () => {
  const files = ["app/admin/platform/roles/RolesMatrix.tsx", "app/admin/platform/roles/messages.ts",
    "app/admin/platform/roles/page.tsx"];
  for (const f of files) {
    const src = readFileSync(new URL(`../${f}`, import.meta.url), "utf8");
    // Strings only (comments may cite the spec task); a quoted string must not carry an issue id or an E2E address.
    const strings = src.match(/"[^"\n]*"|`[^`\n]*`/g) ?? [];
    for (const s of strings) {
      assert.ok(!/AA-\d+/.test(s), `${f}: ${s}`);
      assert.ok(!/e2e[-@]|@adventure\.asia|@e2e-mail/i.test(s), `${f}: ${s}`);
    }
  }
});

test("4. capability columns, areas and the filter", () => {
  const rows = matrixRows(DATA);
  assert.deepEqual(cisCapabilityColumns(rows), ["content.read", "staff.read", "tenant.read"]);
  assert.equal(capabilityArea("tenant.book.read"), "tenant");
  const desk = rows.find((r) => r.code === "custom_desk");
  assert.ok(matchesFilter(desk, "TENANT") && matchesFilter(desk, "desk") && matchesFilter(desk, " "));
  assert.ok(!matchesFilter(desk, "billing"));
});

test("5. the Staff page pickers: active custom roles by name, labels of archived ones, the row's CIS role", async () => {
  const { cisRoleOf, customRoleLabel, customRoleOptions } = await import("./rolesMatrix.ts");
  assert.deepEqual(customRoleOptions(DATA, "cis").map((o) => o.code), ["custom_bot", "custom_desk", "custom_e2e_probe"]);
  assert.deepEqual(customRoleOptions(DATA, "booking"), [{ code: "custom_ops_books", label: "Ops and books" }]);
  assert.deepEqual(customRoleOptions(undefined, "cis"), []);
  assert.equal(customRoleLabel(DATA, "cis", "custom_old"), "Old desk");     // no issue id in the UI
  assert.equal(customRoleLabel(DATA, "cis", "custom_none"), null);
  assert.equal(cisRoleOf({ role: "content_lead", custom_role: "custom_desk" }), "custom_desk");
  assert.equal(cisRoleOf({ role: "content", custom_role: null }), "content");
});
