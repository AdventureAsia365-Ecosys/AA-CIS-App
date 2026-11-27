// frontend/lib/myWork.test.mjs
// AA-1156 — My work (lib/myWork.ts, the /admin/my-work page rule and landing in lib/rbac.ts). Checks:
//   1. every role that sees a tile (holds its capability) may open the tile's target page, and may open My work;
//   2. a role with no tile capability cannot open My work (shown by capability, never by person);
//   3. admin and the content roles land on My work (AA-1190), the viewer on the Review Queue, a custom ops.read role
//      on My work; every landing page is allowed for its role (the proxy redirect cannot loop);
//   4. tileHref adds the backend's filter as a stable query string; unknown keys are not drawn;
//   5. the target pages read the filter the backend sends (?has_draft= and ?status= on Supply copy, ?status= on AA
//      content and Place cards, AA-1190: ?policy= on the Review Queue, ?pending=&app= on Settings, ?tab= on
//      Compliance readiness and Data erasure) or open on it by default (Review Queue: pending; Compliance: open);
//   6. AA-1190: the bands, the collapsed empty queues and the total without the watch tiles.
// Run: npm run test:unit.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { MY_WORK_TILES, knownTiles, splitBands, tileDef, tileHref, totalWaiting } from "./myWork.ts";
import { STAFF_ROLES, can, canSeePage, landingPage } from "./rbac.ts";
import { filtersFromSearch, listUrl } from "./supplyContent.ts";
import { openProposalCount, withOpenProposal } from "./settingsRegistry.ts";
import { policyFromSearch, reviewLinkFromSearch } from "./reviewPolicy.ts";
import { erasureTabFromSearch } from "./erasureView.ts";

const FRONTEND = fileURLToPath(new URL("..", import.meta.url));
const read = (rel) => readFileSync(join(FRONTEND, rel), "utf8");

test("1. a role that sees a tile may open its target page and My work", () => {
  for (const role of STAFF_ROLES) {
    for (const t of MY_WORK_TILES) {
      if (!can(role, t.capability)) continue;
      assert.ok(canSeePage(role, t.path), `${role} sees ${t.key} but may not open ${t.path}`);
      assert.ok(canSeePage(role, "/admin/my-work"), `${role} sees ${t.key} but may not open My work`);
    }
  }
});

test("2. My work opens to exactly the roles with a tile", () => {
  const withTile = STAFF_ROLES.filter((r) => MY_WORK_TILES.some((t) => can(r, t.capability)));
  const page = STAFF_ROLES.filter((r) => canSeePage(r, "/admin/my-work"));
  assert.deepEqual(page, withTile);
  for (const r of ["viewer", "finance", "support", "customer_success", "ai_ops"]) assert.ok(!page.includes(r), r);
});

test("3. landing pages: admin and content roles My work, viewer Review Queue; never a refused page", () => {
  for (const r of ["admin", "content", "content_lead", "tenant_qa"]) assert.equal(landingPage(r), "/admin/my-work", r);
  assert.equal(landingPage({ role: "custom_ops", capabilities: ["ops.read"] }), "/admin/my-work");
  assert.ok(canSeePage({ role: "custom_ops", capabilities: ["ops.read"] }, "/admin/my-work"));
  assert.equal(landingPage("viewer"), "/admin/review");
  for (const r of STAFF_ROLES) assert.ok(canSeePage(r, landingPage(r)), `${r} lands on ${landingPage(r)}`);
});

test("4. tileHref and knownTiles", () => {
  const def = tileDef("supply_drafts");
  assert.equal(tileHref(def, { has_draft: "1" }), "/admin/supply-content?has_draft=1");
  assert.equal(tileHref(tileDef("review_waiting"), { policy: "none" }), "/admin/review?policy=none");
  assert.equal(tileHref(tileDef("settings_to_approve"), { pending: "1", app: "cis" }),
    "/admin/platform/settings?app=cis&pending=1");
  assert.equal(tileHref({ path: "/x" }, { b: "2", a: "1" }), "/x?a=1&b=2");
  const shown = knownTiles({ role: "content", generated_at: "", tiles: [
    { key: "review_waiting", capability: "review.act", filter: {}, available: true, count: 3, oldest_at: null },
    { key: "a_future_tile", capability: "review.act", filter: {}, available: true, count: 9, oldest_at: null },
    { key: "aa_content_drafts", capability: "content.write", filter: { status: "draft" }, available: false,
      count: null, oldest_at: null },
  ] });
  assert.deepEqual(shown.map((s) => s.tile.key), ["review_waiting", "aa_content_drafts"]);
  assert.equal(totalWaiting(shown.map((s) => s.tile)), 3);
  assert.deepEqual(knownTiles(null), []);
});

test("5. the target pages read the tile's filter, or open on it by default", () => {
  assert.match(read("app/admin/supply-content/SupplyContentList.tsx"), /useLinkFilters\(filtersFromSearch\)/);
  assert.match(read("lib/supplyContent.ts"), /export function filtersFromSearch/);
  // the drafts tile sends ?has_draft=1: the page reads it and sends it on to the API
  assert.deepEqual(filtersFromSearch("?has_draft=1"), { status: "", type: "", hasDraft: "1" });
  assert.equal(filtersFromSearch("?has_draft=yes").hasDraft, "");
  assert.match(listUrl(filtersFromSearch("?has_draft=1"), 0, 25), /[?&]has_draft=1&/);
  assert.match(read("app/admin/aa-content/AaContentConsole.tsx"), /useLinkFilters\(filtersFromSearch\)/);
  assert.match(read("app/admin/places/cards/page.tsx"), /useLinkFilters\(placeCardsViewFromSearch\)/);
  assert.match(read("app/admin/review/page.tsx"), /useState\("pending"\)/);
  assert.match(read("app/admin/tenant-qa/compliance/page.tsx"), /useState<StateTab>\("open"\)/);
});

test("5b. AA-1190 target pages read the new filters", () => {
  assert.equal(policyFromSearch("?policy=none"), "none");
  assert.equal(policyFromSearch("?policy=elephant_riding"), "elephant_riding");
  assert.equal(policyFromSearch("?policy=nope"), "all");
  assert.equal(policyFromSearch(""), "all");
  assert.deepEqual(reviewLinkFromSearch("?policy=none"), { policy: "none", tourId: null });
  assert.deepEqual(reviewLinkFromSearch("policy=elephant_riding&tour_id=t-1"), { policy: "elephant_riding", tourId: "t-1" });
  assert.deepEqual(reviewLinkFromSearch("?tour_id="), { policy: "all", tourId: null });
  assert.match(read("app/admin/review/page.tsx"), /reviewLinkFromSearch\(useSearchParams\(\)\.toString\(\)\)/);
  assert.match(read("app/admin/platform/settings/SettingsRegistry.tsx"), /params\.get\("pending"\) === "1"/);
  const items = [{ proposals: [] }, { proposals: [{ id: "a" }, { id: "b" }] }, { proposals: [{ id: "c" }] }];
  assert.equal(withOpenProposal(items).length, 2);
  assert.equal(openProposalCount(items), 3);
  assert.match(read("app/admin/platform/compliance/CompliancePage.tsx"), /get\("tab"\)/);
  assert.equal(erasureTabFromSearch("?tab=offboarding"), "offboarding");
  assert.match(read("app/admin/tenants/data-erasure/DataErasure.tsx"), /useLinkFilters\(erasureTabFromSearch\)/);
});

test("6. AA-1190 bands: work first by band, empty queues folded, watch tiles outside the total", () => {
  const tile = (key, group, count, extra = {}) => ({ key, capability: "x", group, filter: {}, available: true, count,
    oldest_at: null, ...extra });
  const rows = knownTiles({ role: "admin", generated_at: "", tiles: [
    tile("settings_to_approve", "approve", 0),
    tile("review_waiting", "approve", 4),
    tile("supply_drafts", "work", 2),
    tile("aa_content_drafts", "work", 0),
    tile("ops_open_problems", "platform", 0, { available: false, count: null }),
    tile("place_cards_to_review", "watch", 4859, { counts_toward_total: false }),
  ] });
  const { bands, empty } = splitBands(rows);
  assert.deepEqual(bands.map((b) => [b.group, b.tiles.map((r) => r.tile.key)]), [
    ["approve", ["review_waiting"]], ["work", ["supply_drafts"]], ["platform", ["ops_open_problems"]],
    ["watch", ["place_cards_to_review"]],
  ]);
  assert.deepEqual(empty.map((r) => r.tile.key), ["settings_to_approve", "aa_content_drafts"]);
  assert.equal(totalWaiting(rows.map((r) => r.tile)), 6, "place cards are backlog, not in the total");
  // a tile from an older backend (no group) is drawn in the work band
  const old = splitBands(knownTiles({ role: "content", generated_at: "", tiles: [
    { key: "supply_drafts", capability: "content.write", filter: {}, available: true, count: 1, oldest_at: null }] }));
  assert.equal(old.bands[0].group, "work");
});
