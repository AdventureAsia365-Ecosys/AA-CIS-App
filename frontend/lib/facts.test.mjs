// frontend/lib/facts.test.mjs
// AA-814: unit tests for the platform Facts page helpers in facts.ts (list URL, form ↔ body, changed fields, the
// form check, FastAPI error text). Run: npm run test:unit.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  EMPTY_FILTERS, EMPTY_FORM, changedFields, createBody, errorMessage, formOf, formProblem, listUrl,
} from "./facts.ts";

const FACT = {
  fact_id: "fact_abc", scope: "platform", kind: "price", title: "Laos e-visa fee", body: "USD 50.",
  provenance: "Embassy site", country: "Laos", stated_on: "2026-10-01", valid_until: null, updated_by: null,
  created_at: null, updated_at: "2026-10-01T08:00:00+00:00", expired: false,
};

test("listUrl sends only the filters that are set", () => {
  assert.equal(listUrl(EMPTY_FILTERS), "/api/admin/facts");
  assert.equal(listUrl({ kind: "price", q: "  visa ", includeExpired: false }),
    "/api/admin/facts?kind=price&q=visa&include_expired=false");
});

test("createBody collapses spaces and sends empty optionals as null", () => {
  const body = createBody({ ...EMPTY_FORM, kind: "season", title: " Rainy   season ", body: "May to Oct.",
    provenance: "Met", country: "  " });
  assert.deepEqual(body, { kind: "season", title: "Rainy season", body: "May to Oct.", provenance: "Met",
    country: null, stated_on: null, valid_until: null });
});

test("changedFields holds only what differs from the loaded fact", () => {
  assert.deepEqual(changedFields(FACT, formOf(FACT)), {});
  assert.deepEqual(changedFields(FACT, { ...formOf(FACT), body: "USD 55.", country: "" }),
    { body: "USD 55.", country: null });
  assert.deepEqual(changedFields(FACT, { ...formOf(FACT), title: "Laos  e-visa fee " }), {});   // spacing only
});

test("formProblem: required text and the date order", () => {
  assert.equal(formProblem(EMPTY_FORM), "required");
  const ok = formOf(FACT);
  assert.equal(formProblem(ok), null);
  assert.equal(formProblem({ ...ok, valid_until: "2026-09-30" }), "dates");
  assert.equal(formProblem({ ...ok, valid_until: "2026-10-01" }), null);
});

test("errorMessage reads a string detail or a FastAPI validation list", () => {
  assert.equal(errorMessage("Fact not found", 404), "Fact not found");
  assert.equal(errorMessage([{ loc: ["body", "title"], msg: "String should have at least 1 character" }], 422),
    "title: String should have at least 1 character");
  assert.equal(errorMessage(undefined, 500), "Request failed (500)");
});
