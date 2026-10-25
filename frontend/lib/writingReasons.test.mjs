// frontend/lib/writingReasons.test.mjs
// AA-916 WM-07: unit tests for the reason-chip helpers in writingReasons.ts.
// Run: npm run test:unit (node --test with --experimental-strip-types; writingReasons.ts holds only type-level TS).

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import {
  DEFAULT_REASON,
  REASON_CODES,
  REASON_HINT,
  REASON_LABEL,
  commonReason,
  exemplarPath,
  isReasonCode,
  preselect,
  reviewReason,
  toggledPref,
} from "./writingReasons.ts";

test("the codes are the backend's REASON_CODES, in order", () => {
  const py = readFileSync(new URL("../../services/writing_memory/reason_codes.py", import.meta.url), "utf8");
  const literal = py.match(/ReasonCode = Literal\[([^\]]+)\]/s)[1];
  const codes = [...literal.matchAll(/"([a-z_]+)"/g)].map((m) => m[1]);
  assert.deepEqual([...REASON_CODES], codes);
  for (const c of REASON_CODES) {
    assert.ok(REASON_LABEL[c] && REASON_HINT[c], c);
  }
  assert.equal(DEFAULT_REASON, "style_only");
});

test("preselect follows the API suggestion, else style_only", () => {
  assert.equal(preselect("ai_phrasing"), "ai_phrasing");
  assert.equal(preselect("nonsense"), "style_only");
  assert.equal(preselect(undefined), "style_only");
  assert.equal(isReasonCode("fact_not_in_source"), true);
  assert.equal(isReasonCode(3), false);
});

test("an A1 review row preselects from its first known failure code", () => {
  assert.equal(reviewReason(["LOW_QUALITY", "unsupported_number"]), "fact_not_in_source");
  assert.equal(reviewReason(["FORBIDDEN_WORD", "UNSUPPORTED_NUMBER"]), "voice_off_brand");
  assert.equal(reviewReason(["HIGHLIGHTS_OPTIONAL_LANGUAGE"]), "optional_as_included");
  assert.equal(reviewReason(["DAY_OUTPUT_GAP"]), "wrong_place_or_time");
  assert.equal(reviewReason(["SEO_META_TOO_LONG"]), "format_channel");
  assert.equal(reviewReason(["DEGRADED_DETECTOR"]), "style_only");
  assert.equal(reviewReason([]), "style_only");
  assert.equal(reviewReason(null), "style_only");
});

test("bulk: the most common preselection, ties in code order", () => {
  assert.equal(commonReason([["FORBIDDEN_WORD"], ["FABRICATED"], ["BRAND_FLAGGED"]]), "voice_off_brand");
  assert.equal(commonReason([["FORBIDDEN_WORD"], ["FABRICATED"]]), "fact_not_in_source");
  assert.equal(commonReason([]), "style_only");
});

test("a click on the active example toggle clears it", () => {
  assert.equal(toggledPref(null, "use"), "use");
  assert.equal(toggledPref("use", "use"), null);
  assert.equal(toggledPref("use", "never"), "never");
  assert.equal(toggledPref(undefined, "never"), "never");
});

test("the PATCH path names the owner and the signal", () => {
  assert.equal(exemplarPath("pieces", "p-1", "s 2"), "/api/admin/content-quality/pieces/p-1/signals/s%202");
  assert.equal(exemplarPath("tour-versions", "v", "s"), "/api/admin/content-quality/tour-versions/v/signals/s");
});
