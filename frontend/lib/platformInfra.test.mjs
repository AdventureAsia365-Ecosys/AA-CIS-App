// frontend/lib/platformInfra.test.mjs
// Platform › Infra limits: the pure helpers of lib/platformInfra.ts (language pick, readable values, filter, links),
// the BFF rule (admin only), the nav entry, a unit message for every unit and no issue id on the page.
// Run: npm run test:unit.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { INFRA_PATH, docsLink, filterGroups, pick, prettyValue, shortSha } from "./platformInfra.ts";
import { canReadAdminPath, canSeePage } from "./rbac.ts";
import { ADMIN_IA } from "./adminIa.ts";

const item = (key, value, unit, en = key, vi = `vi ${key}`) =>
  ({ key, value, unit, source: `accounts/aa365 variable ${key}`, label: { en, vi }, known: true });
const GROUPS = [
  { group: "waf", title: { en: "Web firewall", vi: "Tường lửa web" }, what: { en: "w", vi: "w" },
    why: { en: "y", vi: "y" }, change: { en: "c", vi: "c" }, editable: null, editable_href: null,
    items: [item("alb_acl_rate_limit_per_5min_per_ip", "1000", "per_5min_ip", "Rate limit of the load balancer"),
      item("geo_allowed_countries", "US, GB, VN", "countries", "Countries")] },
  { group: "alarms", title: { en: "Alarm thresholds", vi: "Ngưỡng cảnh báo" }, what: { en: "w", vi: "w" },
    why: { en: "y", vi: "y" }, change: { en: "c", vi: "c" }, editable: null, editable_href: null,
    items: [item("rds_free_storage_bytes_below", "10737418240", "bytes", "Free storage")] },
];

test("pick takes the viewer's language and falls back to English", () => {
  assert.equal(pick({ en: "Limit", vi: "Giới hạn" }, "vi"), "Giới hạn");
  assert.equal(pick({ en: "Limit", vi: "" }, "vi"), "Limit");
  assert.equal(pick(null, "en"), "");
});

test("readable values: bytes in GiB, whole hours, thousands separators; text stays as written", () => {
  assert.equal(prettyValue({ unit: "bytes", value: "10737418240" }, "en"), "10 GiB");
  assert.equal(prettyValue({ unit: "seconds", value: "14400" }, "en"), "4 h");
  assert.equal(prettyValue({ unit: "seconds", value: "90" }, "en"), null);
  assert.equal(prettyValue({ unit: "per_5min_ip", value: "1000" }, "en"), null);
  assert.equal(prettyValue({ unit: "count", value: "25000" }, "en"), "25,000");
  assert.equal(prettyValue({ unit: "switch", value: "on" }, "en"), null);
  assert.equal(prettyValue({ unit: "text", value: "2 of 3" }, "en"), null);
});

test("the filter matches label, key, value or source and drops empty groups", () => {
  assert.equal(filterGroups(GROUPS, "", "en"), GROUPS);
  const byLabel = filterGroups(GROUPS, "load balancer", "en");
  assert.deepEqual(byLabel.map((g) => g.group), ["waf"]);
  assert.equal(byLabel[0].items.length, 1);
  assert.deepEqual(filterGroups(GROUPS, "VN", "en")[0].items.map((i) => i.key), ["geo_allowed_countries"]);
  assert.deepEqual(filterGroups(GROUPS, "ngưỡng", "vi").map((g) => g.group), ["alarms"]);
  assert.deepEqual(filterGroups(GROUPS, "nothing-like-this", "en"), []);
});

test("links and commit text", () => {
  assert.equal(docsLink({ missing: false, reason: null, path: "docs/generated/alarms.md" }),
    "/admin/docs?path=docs%2Fgenerated%2Falarms.md");
  assert.equal(docsLink({ missing: true, reason: "unavailable" }), null);
  assert.equal(shortSha("09ea0c487275f0b4c702f4d67042c07edbf48015"), "09ea0c4");
  assert.equal(shortSha("not a sha"), "");
  assert.equal(shortSha(null), "");
  assert.equal(INFRA_PATH, "/api/admin/platform/infra");
});

test("only the administrator opens the page and reads its API", () => {
  assert.equal(canSeePage("admin", "/admin/platform/infra"), true);
  assert.equal(canReadAdminPath("admin", "platform/infra"), true);
  for (const role of ["ai_ops", "content_lead", "customer_success", "finance", "viewer"]) {
    assert.equal(canSeePage(role, "/admin/platform/infra"), false, role);
    assert.equal(canReadAdminPath(role, "platform/infra"), false, role);
  }
});

test("the nav lists Platform › Infra limits; every unit has a message; the page shows no issue id", () => {
  const platform = ADMIN_IA.find((g) => g.label === "Platform");
  assert.ok(platform.items.some((i) => i.href === "/admin/platform/infra" && i.icon === "infra"));
  const lib = readFileSync(new URL("./platformInfra.ts", import.meta.url), "utf8");
  const units = [...lib.split("export type InfraUnit =")[1].split(";")[0].matchAll(/"([a-z_0-9]+)"/g)].map((m) => m[1]);
  assert.ok(units.length >= 10);
  const messages = readFileSync(new URL("../app/admin/platform/infra/messages.ts", import.meta.url), "utf8");
  for (const u of units) {
    assert.equal(messages.split(`unit_${u}:`).length - 1, 2, `unit_${u} in English and Vietnamese`);
  }
  for (const f of ["../app/admin/platform/infra/messages.ts", "../app/admin/platform/infra/InfraLimits.tsx"]) {
    const text = readFileSync(new URL(f, import.meta.url), "utf8");
    const strings = text.match(/"[^"\n]*"|`[^`\n]*`/g) ?? [];
    assert.ok(!strings.some((s) => /AA-\d+/.test(s)), f);
  }
});
