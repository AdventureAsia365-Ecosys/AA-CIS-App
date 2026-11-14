// frontend/lib/productCatalogue.test.mjs
// AA-1106: unit tests for the Product map helpers in productCatalogue.ts (invented rows; this repo ships no catalogue).
// Run: npm run test:unit (node --test with --experimental-strip-types).

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  conditionKinds,
  docHref,
  graphEdges,
  graphLayout,
  localName,
  localSummary,
  matchesFeature,
  matchesIntegration,
  roleOptions,
  settingHref,
  summaryCounts,
} from "./productCatalogue.ts";

const feature = (over = {}) => ({
  id: "CIS-F001", app: "cis", module: "tours", name: "Tours", name_vi: "Tour", summary: "Tour pages.",
  roles: ["admin", "content"], status: "live", owner: "content_lead", surface: ["page:/admin/tours"],
  conditions: { settings: ["cis.a"], entitlements: [] }, depends_on: [], guide: null, uat: [], integrations: [],
  ...over,
});
const link = (over = {}) => ({
  id: "INT-001", name: "Catalogue", producer: "cis", consumers: ["crm", "booking"], kind: "http_api",
  contracts: [{ file: "catalog.http.v1.openapi.yaml", version: "1" }], auth: "M2M token",
  conditions: { entitlements: ["trade.book"] }, cadence: "", failure: "", kill_switch: "", personal_data: false,
  features: [], owner: "tech_lead", status: "live", ...over,
});

test("local name and summary follow the language with an English fallback", () => {
  assert.equal(localName(feature(), "vi"), "Tour");
  assert.equal(localName(feature({ name_vi: undefined }), "vi"), "Tours");
  assert.equal(localName(feature(), "en"), "Tours");
  assert.equal(localSummary({ summary: "A", summary_vi: "B" }, "vi"), "B");
  assert.equal(localSummary({ summary: "A" }, "vi"), "A");
});

test("condition kinds list only the non-empty lists", () => {
  assert.deepEqual(conditionKinds(feature().conditions), ["settings"]);
  assert.deepEqual(conditionKinds(undefined), []);
});

test("feature filter: app, status, role, condition kind and search", () => {
  const f = feature();
  assert.ok(matchesFeature(f, {}));
  assert.ok(matchesFeature(f, { app: "cis", status: "live", role: "content", cond: "settings" }));
  assert.ok(!matchesFeature(f, { app: "crm" }));
  assert.ok(!matchesFeature(f, { status: "beta" }));
  assert.ok(!matchesFeature(f, { role: "owner" }));
  assert.ok(!matchesFeature(f, { cond: "secrets" }));
  assert.ok(matchesFeature(f, { q: "CIS.A" }), "searches condition names");
  assert.ok(matchesFeature(f, { q: "/admin/tours" }), "searches the surface");
  assert.ok(!matchesFeature(f, { q: "nothing" }));
});

test("link filter: app is the producer or a consumer", () => {
  const i = link();
  assert.ok(matchesIntegration(i, { app: "cis" }));
  assert.ok(matchesIntegration(i, { app: "booking" }));
  assert.ok(!matchesIntegration(i, { app: "tripplanner" }));
  assert.ok(matchesIntegration(i, { q: "openapi", kind: "http_api", status: "live" }));
  assert.ok(!matchesIntegration(i, { kind: "db_view" }));
});

test("role options are unique and sorted", () => {
  assert.deepEqual(roleOptions([feature(), feature({ roles: ["owner", "admin"] })]), ["admin", "content", "owner"]);
});

test("setting and doc links", () => {
  const settings = { "cis.a": { app: "cis", area: "content" }, "booking.policy.x": { app: "booking", area: "pricing_policy" } };
  assert.equal(settingHref("cis.a", settings), "/admin/platform/settings?app=cis&area=content");
  assert.equal(settingHref("booking.policy.x", settings), "/admin/platform/settings?app=booking");
  assert.equal(settingHref("missing", settings), null);
  assert.equal(docHref("docs/catalogue/README.md"), "/admin/docs?path=docs%2Fcatalogue%2FREADME.md");
});

test("graph edges merge links per pair and leave retired links out", () => {
  const edges = graphEdges([
    link(),
    link({ id: "INT-002", consumers: ["crm"], status: "beta" }),
    link({ id: "INT-003", status: "retired" }),
  ]);
  assert.deepEqual(edges, [
    { from: "cis", to: "booking", ids: ["INT-001"], live: 1 },
    { from: "cis", to: "crm", ids: ["INT-001", "INT-002"], live: 1 },
  ]);
});

test("graph layout puts the first node on top, inside the box", () => {
  const pos = graphLayout(["a", "b", "c", "d"], 400, 300);
  assert.equal(pos.a.x, 200);
  assert.ok(pos.a.y < 150);
  for (const p of Object.values(pos)) assert.ok(p.x >= 0 && p.x <= 400 && p.y >= 0 && p.y <= 300);
});

test("summary counts", () => {
  const c = summaryCounts({
    apps: [{ id: "cis", name: "CIS" }], features: [feature(), feature({ id: "CIS-F002", status: "planned" })],
    integrations: [link(), link({ id: "INT-9", status: "retired" })], journeys: [], scenarios: { "CIS-01": {} },
  });
  assert.deepEqual(c, { apps: 1, features: 2, live: 1, links: 1, journeys: 0, uat: 1 });
});
