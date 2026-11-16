// frontend/lib/researchPanel.test.mjs — AA-1122: the pure parts of the SEO Intelligence Research tab.
import assert from "node:assert/strict";
import { test } from "node:test";

import { marketRows, monthState, monthWarn, runScope, usd } from "./researchPanel.ts";

test("usd: three decimals under a dollar, two above, a dash when unknown", () => {
  assert.equal(usd(0.0456), "$0.046");
  assert.equal(usd(4.2), "$4.20");
  assert.equal(usd(null), "—");
  assert.equal(usd(Number.NaN), "—");
});

test("monthWarn: at the warning share of the cap, never for a 0 or missing cap", () => {
  assert.equal(monthWarn({ cap_usd: 5, spent_usd: 4, warn_pct: 80 }), true);
  assert.equal(monthWarn({ cap_usd: 5, spent_usd: 3.9, warn_pct: 80 }), false);
  assert.equal(monthWarn({ cap_usd: 0, spent_usd: 0, warn_pct: 80 }), false);
  assert.equal(monthWarn(null), false);
});

test("marketRows: stale count per market and the new-market flag", () => {
  assert.deepEqual(marketRows({ US: 3, DE: 900 }, ["DE"]), [
    { market: "US", stale: 3, isNew: false }, { market: "DE", stale: 900, isNew: true }]);
  assert.deepEqual(marketRows(undefined, undefined), []);
});

test("runScope: codes upper-cased, blanks dropped, a cap only when positive", () => {
  assert.deepEqual(runScope(" us, uk ,", "1.5"), { markets: ["US", "UK"], max_usd: 1.5 });
  assert.deepEqual(runScope("US", "0"), { markets: ["US"] });
  assert.deepEqual(runScope("US", "abc"), { markets: ["US"] });
});

test("monthState: over at the hard cap, warn at the warning share, nothing without a cap (S282)", () => {
  assert.equal(monthState({ cap_usd: 5, spent_usd: 12.53, warn_pct: 80 }), "over");
  assert.equal(monthState({ cap_usd: 5, spent_usd: 5, warn_pct: 80 }), "over");
  assert.equal(monthState({ cap_usd: 5, spent_usd: 4.2, warn_pct: 80 }), "warn");
  assert.equal(monthState({ cap_usd: 5, spent_usd: 1, warn_pct: 80 }), null);
  assert.equal(monthState({ cap_usd: 0, spent_usd: 9, warn_pct: 80 }), null);
  assert.equal(monthState(null), null);
});
