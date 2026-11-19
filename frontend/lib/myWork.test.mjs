// frontend/lib/myWork.test.mjs
// AA-1156 — My work (lib/myWork.ts, the /admin/my-work page rule and landing in lib/rbac.ts). Checks:
//   1. every role that sees a tile (holds its capability) may open the tile's target page, and may open My work;
//   2. a role with no tile capability cannot open My work (shown by capability, never by person);
//   3. the content roles land on My work, admin on Overview, the viewer on the Review Queue; every landing page is
//      allowed for its role (the proxy redirect cannot loop);
//   4. tileHref adds the backend's filter as a stable query string; unknown keys are not drawn;
//   5. the target pages read the filter the backend sends (?status= on Supply copy, AA content, Place cards) or open on
//      it by default (Review Queue: pending; Compliance: open).
// Run: npm run test:unit.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { MY_WORK_TILES, knownTiles, tileDef, tileHref, totalWaiting } from "./myWork.ts";
import { STAFF_ROLES, can, canSeePage, landingPage } from "./rbac.ts";

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

test("3. landing pages: content roles My work, admin Overview, viewer Review Queue; never a refused page", () => {
  for (const r of ["content", "content_lead", "tenant_qa"]) assert.equal(landingPage(r), "/admin/my-work", r);
  assert.equal(landingPage("admin"), "/admin/overview");
  assert.equal(landingPage("viewer"), "/admin/review");
  for (const r of STAFF_ROLES) assert.ok(canSeePage(r, landingPage(r)), `${r} lands on ${landingPage(r)}`);
});

test("4. tileHref and knownTiles", () => {
  const def = tileDef("supply_drafts");
  assert.equal(tileHref(def, { status: "in_review" }), "/admin/supply-content?status=in_review");
  assert.equal(tileHref(tileDef("review_waiting"), {}), "/admin/review");
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
  assert.match(read("app/admin/supply-content/SupplyContentList.tsx"), /initialFilters\(hydrated\)/);
  assert.match(read("lib/supplyContent.ts"), /export function filtersFromSearch/);
  assert.match(read("app/admin/aa-content/AaContentConsole.tsx"), /filtersFromSearch\(/);
  assert.match(read("app/admin/places/cards/page.tsx"), /status: p\.get\("status"\)/);
  assert.match(read("app/admin/review/page.tsx"), /useState\("pending"\)/);
  assert.match(read("app/admin/tenant-qa/compliance/page.tsx"), /useState<StateTab>\("open"\)/);
});
