// frontend/lib/aa1127Overflow390.test.mjs
// AA-1127 (S285 live evidence run at 390 px, read-only E2E CIS admin): five spots of the CIS admin that ran past a
// phone or said the wrong thing. Guards the source against each one coming back. Run: npm run test:unit.
//
//   1. Platform › Settings search: the Area column came first, so on a phone (the kit table's first column is the card
//      title) the whole setting cell sat in a right-aligned label/value row and was cut on the right (+58 px).
//   2. Jev Decisions: the colour legend at the bottom was one flex row of 8 pills (+276 px).
//   3. Tenant QA review log: check-id badges ("structural:t2_day_count_vs_master") never broke (+20 px).
//   4. Content Trace (and every inline TenantSelect): the search placeholder was cut in its 200 px box.
//   5. App profiles: "Takes effect" said "(TripPlanner within 20 s)" on the CIS, CRM and Booking rows too.
// Measured before / after with a local next dev harness (fixtures only, no backend): frame overflow 0 at 390 in light
// and dark on the four pages; docs/implementation-notes/AA-1127-l21-390.md.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { PROFILE_APPS, appliesKey } from "./aiGateway.ts";

const read = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), "utf8");
/** Source without comments, so a comment that quotes a style never satisfies a check. */
const code = (rel) => read(rel)
  .replace(/\/\*[\s\S]*?\*\//g, "")
  .replace(/^\s*\/\/.*$/gm, "")
  .replace(/(^|[^:"'`])\/\/.*$/gm, "$1");

test("Platform › Settings search: the setting column stays first (the phone card title), Area right after it", () => {
  const src = code("../app/admin/platform/settings/SettingsTable.tsx");
  const cols = src.slice(src.indexOf("const cols = useMemo"), src.indexOf("if (groups.length === 0)"));
  const first = cols.indexOf("...columns.slice(0, 1)");
  const area = cols.indexOf('id: "area"');
  const rest = cols.indexOf("...columns.slice(1)");
  assert.ok(first > 0 && area > first && rest > area, "search columns: setting, area, then the rest");
});

test("Platform › Settings: the setting cell never asks for more than its column; label and key wrap anywhere", () => {
  const src = code("../app/admin/platform/settings/SettingGuide.tsx");
  const cell = src.slice(src.indexOf("export function SettingCell("), src.indexOf("export function DraftNow("));
  assert.match(cell, /minWidth: "min\(240px, 100%\)"/);
  assert.doesNotMatch(cell, /minWidth: 240[,\s}]/, "a bare 240 px minimum is wider than a phone card's value row");
  assert.match(cell, /<strong style=\{\{[^}]*overflowWrap: "anywhere"[^}]*\}\}>\{item\.label\}/);
  assert.match(cell, /<span style=\{\{[^}]*overflowWrap: "anywhere"[^}]*\}\}>\{item\.key\}/);
});

test("Jev Decisions: the colour legend wraps", () => {
  const src = code("../app/admin/decisions/page.tsx");
  const legend = src.slice(src.indexOf('data-testid="decisions-legend"'));
  assert.ok(legend.length > 0, "the legend keeps its test id");
  assert.match(legend.slice(0, 300), /display: "flex", flexWrap: "wrap"/);
});

test("Tenant QA review log: every badge or label holding a check id or gate key may break anywhere", () => {
  const src = code("../app/admin/tenant-qa/review-log/page.tsx");
  assert.match(src, /const tokenBadge: React\.CSSProperties = \{ maxWidth: "100%", overflowWrap: "anywhere" \}/);
  assert.match(src, /<Badge key=\{i\} tone="neutral" style=\{tokenBadge\}>\{item\.check_id\}<\/Badge>/);
  assert.match(src, /<Badge key=\{checkId\} tone=\{[^}]*\} style=\{tokenBadge\}>/);
  // the fixed-width gate labels of both gate lists
  assert.equal(src.match(/\.\.\.tokenText, width: 1[56]0, flexShrink: 0/g)?.length, 2);
});

test("TenantSelect: the search placeholder fits the 200 px filter-bar box (EN and VI)", () => {
  const msgs = read("../app/admin/_components/components.messages.ts");
  const found = [...msgs.matchAll(/searchTenants: "([^"]+)"/g)].map((m) => m[1]);
  assert.equal(found.length, 2, "English and Vietnamese");
  // ~7 px a character at the kit sm input size; 200 px less padding leaves room for about 22 characters
  for (const text of found) assert.ok(text.length <= 18, `"${text}" is cut in the inline search box`);
  assert.match(code("../app/admin/_components/TenantSelect.tsx"), /inlineSearchStyle: React\.CSSProperties = \{ width: 200/);
});

test("App profiles: 'Takes effect' is worded per app", () => {
  assert.equal(appliesKey({ app: "cis", enforced_here: true }), "appliesCis");
  assert.equal(appliesKey({ app: "tripplanner", enforced_here: true }), "appliesTripplanner");
  assert.equal(appliesKey({ app: "crm", enforced_here: true }), "appliesCrm");
  assert.equal(appliesKey({ app: "booking", enforced_here: true }), "appliesHere");
  for (const app of PROFILE_APPS) assert.equal(appliesKey({ app, enforced_here: false }), "appliesLater");

  const msgs = read("../app/admin/ai-gateway/appProfiles.messages.ts");
  for (const key of ["appliesCis", "appliesTripplanner", "appliesCrm", "appliesHere", "appliesLater"]) {
    assert.equal(msgs.match(new RegExp(`\\b${key}:`, "g"))?.length, 2, `${key}: English and Vietnamese`);
  }
  // only the TripPlanner line names TripPlanner
  const lines = msgs.split("\n").filter((l) => /^\s*applies\w+:/.test(l));
  for (const l of lines) {
    if (!/appliesTripplanner:/.test(l)) assert.doesNotMatch(l, /TripPlanner/, l.trim());
  }
  const card = code("../app/admin/ai-gateway/AppProfilesCard.tsx");
  assert.match(card, /t\(appliesKey\(x\.row\.original\)\)/);
  assert.doesNotMatch(card, /t\("appliesHere"\)/);
});
