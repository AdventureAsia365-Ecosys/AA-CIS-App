// frontend/lib/overviewB2bOnly.test.mjs
// AA-1034 R0 (root ADR 0019, B2B-only model): the admin Overview shows no B2C card. The TripPlanner customers card
// and the Booking funnel of traveller inquiries stay in the repo (no page renders them since AA-1140 made /admin/overview
// the Ecosystem status page), so they come back if the decision is reversed. Source guards. Run: npm run test:unit.

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

test("no admin page renders a B2C card (the components stay in the repo for a reversal)", () => {
  // AA-1140: /admin/overview became the Ecosystem status page; the B2C cards are rendered nowhere.
  for (const rel of ["overview/page.tsx", "data-coverage/page.tsx", "data-coverage/ContentPipeline.tsx",
    "tenants/page.tsx", "tenants/TenantActivity.tsx"]) {
    const page = code(rel);
    for (const card of ["<TripPlannerCard ", "<BookingFunnelCard "]) {
      assert.equal(page.indexOf(card), -1, `${card.trim()} must not render in ${rel}`);
    }
  }
  for (const card of ["TripPlannerCard", "BookingFunnelCard"]) {
    assert.ok(code(`overview/_components/${card}.tsx`).includes(`export function ${card}`), `${card} stays`);
  }
});

test("the Overview description no longer promises TripPlanner customers or the Booking funnel", () => {
  const messages = code("overview/messages.ts");
  assert.doesNotMatch(messages, /TripPlanner customers|Booking funnel|khách hàng TripPlanner|phễu Booking/);
});
