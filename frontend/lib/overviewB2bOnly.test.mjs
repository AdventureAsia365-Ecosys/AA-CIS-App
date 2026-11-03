// frontend/lib/overviewB2bOnly.test.mjs
// AA-1034 R0 (root ADR 0019, B2B-only model): the admin Overview shows no B2C card. The TripPlanner customers card
// and the Booking funnel of traveller inquiries stay in the repo behind SHOW_B2C_CARDS (off), so they come back with
// one line if the decision is reversed. Source guards. Run: npm run test:unit.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const APP = fileURLToPath(new URL("../app/admin/", import.meta.url));

/** Source without comments, so a comment that quotes a style never satisfies or trips a check. */
function code(rel) {
  return readFileSync(APP + rel, "utf8")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/\{\/\*[\s\S]*?\*\/\}/g, "")
    .replace(/^\s*\/\/.*$/gm, "")
    .replace(/(^|[^:"'`])\/\/.*$/gm, "$1");
}

test("the B2C cards are switched off", () => {
  const page = code("overview/page.tsx");
  assert.match(page, /const SHOW_B2C_CARDS = false;/);
  assert.doesNotMatch(page, /export const SHOW_B2C_CARDS/, "a page may export only Next.js page fields");
});

test("every B2C card render sits behind SHOW_B2C_CARDS", () => {
  const page = code("overview/page.tsx");
  const gate = page.indexOf("{SHOW_B2C_CARDS && (");
  assert.ok(gate > 0, "the B2C card block must be gated");
  for (const card of ["<TripPlannerCard ", "<BookingFunnelCard "]) {
    const at = page.indexOf(card);
    assert.ok(at > gate, `${card.trim()} must render inside the gated block`);
    assert.equal(page.indexOf(card, at + 1), -1, `${card.trim()} must render once`);
  }
});

test("the Overview description no longer promises TripPlanner customers or the Booking funnel", () => {
  const messages = code("overview/messages.ts");
  assert.doesNotMatch(messages, /TripPlanner customers|Booking funnel|khách hàng TripPlanner|phễu Booking/);
});
