// AA-1127 (C1, S283 UI audit): numbers that disagree between pages read one source.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { needsLook, statusWordKey } from "./aiGateway.ts";

test("Need a look follows the row badge: an idle LLM or guard row is not counted", () => {
  const rows = [
    { group: "llm", status: "unknown", status_reason: null },           // No recent calls
    { group: "guard", status: "unknown", status_reason: "" },           // No recent calls
    { group: "llm", status: "open", status_reason: "circuit_open" },
    { group: "jev", status: "low", status_reason: "below_threshold" },
    { group: "jev", status: "unknown", status_reason: "no top-up known" },
    { group: "llm", status: "ok", status_reason: null },
  ];
  assert.equal(needsLook(rows), 3);
  for (const r of rows) {
    const counted = needsLook([r]) === 1;
    const quiet = statusWordKey(r) === "no_recent_calls";
    if (quiet) assert.equal(counted, false, JSON.stringify(r));
  }
});

test("Supply content: one blockers list drives the box and the Publish button", () => {
  const src = readFileSync(new URL("../app/admin/supply-content/SupplyContentDetail.tsx", import.meta.url), "utf8");
  assert.match(src, /const draftBlockers = draftBlockersOf\(entity, draft, media, st\.words\);/);
  assert.match(src, /return draft \? publishBlockers\(entity, draft, media, words\) : \[\];/);
  assert.match(src, /<Blockers items=\{draftBlockers\} \/>/);
  assert.match(src, /disabled=\{draftBlockers\.length > 0\}/);
  assert.equal((src.match(/publishBlockers\(entity, draft,/g) || []).length, 1);
  assert.match(src, /title=\{whenBlocked\(draftBlockers, t\("publishBlockedTitle"\)\)\}/);
});

test("Tenant 360: the Tours label reads the API total and the tab pages on the server", () => {
  const src = readFileSync(new URL("../app/admin/tenants/_components/TenantDetail.tsx", import.meta.url), "utf8");
  assert.match(src, /label: tr\("tabTours", toursTotal\(data\)\)/);
  assert.match(src, /return d\.pagination\?\.total \?\? d\.rewritten_tours\.length;/);
  assert.match(src, /details\?page=\$\{toursPage\}&page_size=\$\{TOURS_PAGE_SIZE\}/);
  assert.match(src, /<Pagination page=\{page\} total=\{total\} pageSize=\{TOURS_PAGE_SIZE\} onPage=\{onPage\} \/>/);
  assert.doesNotMatch(src, /tr\("tabTours", data\.rewritten_tours\.length\)/);
});

test("Reference prices: every status the API counts as waiting has a filter (Suggested included)", async () => {
  const { FILTERS } = await import("./referencePrices.ts");
  assert.ok(FILTERS.includes("suggested"));
  const backend = readFileSync(new URL("../../services/reference_price/store.py", import.meta.url), "utf8");
  const statuses = backend.match(/STATUSES: tuple\[str, \.\.\.\] = \(([^)]*)\)/)[1].match(/"([a-z_]+)"/g).map((s) => s.slice(1, -1));
  // every status but "missing" (reached through "No price") is a filter of the page
  for (const s of statuses.filter((x) => x !== "missing")) assert.ok(FILTERS.includes(s), s);
});

test("SEO intelligence: the Keywords tab opens on linked keywords and the demand bar shows linked volume", () => {
  const src = readFileSync(new URL("../app/admin/seo-intelligence/page.tsx", import.meta.url), "utf8");
  assert.match(src, /const \[linkedOnly, setLinkedOnly\] = useState\(true\);/);
  assert.match(src, /data=\{shownKeywords\(data\.keywords, linkedOnly\)\}/);
  assert.match(src, /return linkedOnly \? rows\.filter\(\(r\) => r\.used_in_content\) : rows;/);
  assert.match(src, /m\.linked_volume \?\? m\.total_volume/);
});
