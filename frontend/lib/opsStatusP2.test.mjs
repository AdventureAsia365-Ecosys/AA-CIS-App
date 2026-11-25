// AA-1142 (S292): the pure helpers of the P2 features of the Ecosystem status page (lib/opsStatusP2.ts): burn and
// budget levels, external target levels, incident tones and titles, the tenant filter. Fixtures are invented.
// Run: npm run test:unit.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  agencyLabel, budgetBar, budgetLevel, burnLevel, externalLevel, fmtBurn, incidentTone, p2Sections, tenantParam,
  tenantsQuery, titleFromItem, titleOk,
} from "./opsStatusP2.ts";

test("burn and budget levels: not judged is grey, never green", () => {
  assert.equal(burnLevel(undefined), "none");
  assert.equal(burnLevel({ burn: 20, events: 400, bad: 40, factor: 14.4, state: "alert" }), "crit");
  assert.equal(burnLevel({ burn: 20, events: 10, bad: 4, factor: 14.4, state: "few" }), "none");
  assert.equal(burnLevel({ burn: 1, events: 400, bad: 2, factor: 14.4, state: "ok" }), "ok");
  assert.equal(budgetLevel(null), "none");
  assert.equal(budgetLevel(-3), "crit");
  assert.equal(budgetLevel(10), "warn");
  assert.equal(budgetLevel(60), "ok");
  assert.equal(budgetBar(-40), 0);
  assert.equal(budgetBar(140), 100);
  assert.equal(budgetBar(null), 0);
});

test("burn rate format", () => {
  assert.equal(fmtBurn(null), "—");
  assert.equal(fmtBurn(2.14), "2.1×");
  assert.equal(fmtBurn(46.2), "46×");
});

test("external levels fold in the certificate", () => {
  assert.equal(externalLevel({ state: "ok", cert_level: "ok" }), "ok");
  assert.equal(externalLevel({ state: "ok", cert_level: "warn" }), "warn");
  assert.equal(externalLevel({ state: "ok", cert_level: "crit" }), "crit");
  assert.equal(externalLevel({ state: "down", cert_level: "none" }), "crit");
  assert.equal(externalLevel({ state: "unknown", cert_level: "none" }), "none");
});

test("incident tone, title rules", () => {
  assert.equal(incidentTone("resolved"), "success");
  assert.equal(incidentTone("monitoring"), "warning");
  assert.equal(incidentTone("investigating"), "danger");
  assert.equal(titleFromItem("  Zz api fails  "), "Zz api fails");
  assert.equal(titleFromItem("x".repeat(400)).length, 300);
  assert.equal(titleOk("zz"), false);
  assert.equal(titleOk(" zz api "), true);
});

test("tenant filter and query", () => {
  const id = "00000000-0000-4000-8000-0000000000AA";
  assert.equal(tenantParam(id), id.toLowerCase());
  assert.equal(tenantParam("zz"), null);
  assert.equal(tenantParam(null), null);
  assert.equal(tenantsQuery("6h", "", "", null), "range=6h");
  assert.equal(tenantsQuery("custom", "2026-11-25T01:00:00Z", "2026-11-25T02:00:00Z", "a"),
    "range=custom&from=2026-11-25T01%3A00%3A00Z&to=2026-11-25T02%3A00%3A00Z&tenant=a");
  assert.equal(agencyLabel({ name: null, slug: null, tenant_id: id }), "00000000…");
  assert.equal(agencyLabel({ name: "Zz Travel", slug: "zz", tenant_id: id }), "Zz Travel");
});

test("P2 sections are optional on an older answer", () => {
  assert.deepEqual(p2Sections({ sections: {} }), { slo: undefined, external: undefined });
  const slo = { status: "ok", data: { rows: [] } };
  assert.equal(p2Sections({ sections: { slo } }).slo, slo);
});
