// frontend/lib/referencePrices.test.mjs
// AA-1091: unit tests for the Reference prices page helpers in referencePrices.ts (list / export URL, the inline-edit
// draft, its check and save body, accept, the import lines shown, base64, FastAPI error text). Run: npm run test:unit.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  EMPTY_FILTERS, amountOf, canAccept, draftChanged, draftOf, draftProblem, errorMessage, exportUrl, fromBase64,
  linesToShow, listUrl, saveBody, toBase64, trimZeros,
} from "./referencePrices.ts";

const SAVED = {
  amount: "1450.00", currency: "USD", basis: "per_person", origin: "staff", note: "High season", needs_review: false,
  review_reasons: [], price_raw_seen: "USD 1,450 pp", reviewed_by: "admin:a", reviewed_at: "2026-11-12T08:00:00+00:00",
  updated_by: "admin:a", updated_at: "2026-11-12T08:00:00+00:00",
};
const ITEM = {
  source_key: "k1", tour_id: "t1", name: "Invented Ridge Trek", country: "Laos", duration: "5 days", live: true,
  price_raw: "USD 1,450 pp", status: "confirmed", text_changed: false, saved: SAVED, suggestion: null,
  parse_reasons: [],
};
const SUGGESTED = {
  ...ITEM, status: "suggested", saved: null,
  suggestion: { amount: "990.00", currency: "EUR", basis: "from", reasons: [], needs_review: false },
};

test("listUrl and exportUrl carry the filter, the page only on the list", () => {
  assert.equal(listUrl(EMPTY_FILTERS, { index: 0, size: 50 }),
    "/api/admin/reference-prices?status=no_price&page=1&page_size=50");
  const f = { status: "all", country: "Laos", q: "  ridge ", liveOnly: false };
  assert.equal(listUrl(f, { index: 2, size: 100 }),
    "/api/admin/reference-prices?status=all&country=Laos&q=ridge&live_only=false&page=3&page_size=100");
  assert.equal(exportUrl(f), "/api/admin/reference-prices/export?status=all&country=Laos&q=ridge&live_only=false");
});

test("draftOf starts from the saved price, else the suggestion, else empty", () => {
  assert.deepEqual(draftOf(ITEM), { amount: "1450", currency: "USD", basis: "per_person", note: "High season" });
  assert.deepEqual(draftOf(SUGGESTED), { amount: "990", currency: "EUR", basis: "from", note: "" });
  assert.deepEqual(draftOf({ ...SUGGESTED, suggestion: null }),
    { amount: "", currency: "USD", basis: "per_person", note: "" });
  assert.equal(trimZeros("1450.50"), "1450.50");
});

test("amountOf reads the usual ways of typing an amount", () => {
  assert.equal(amountOf("1,450"), 1450);
  assert.equal(amountOf("1.450,50"), 1450.5);
  assert.equal(amountOf("1,450.50"), 1450.5);
  assert.equal(amountOf(" 990 "), 990);
  assert.equal(amountOf("12,5"), 12.5);
  assert.equal(amountOf("abc"), null);
  assert.equal(amountOf(""), null);
});

test("draftProblem names the first bad field", () => {
  const ok = { amount: "990", currency: "usd", basis: "from", note: "" };
  assert.equal(draftProblem(ok), null);
  assert.equal(draftProblem({ ...ok, amount: "0" }), "errAmount");
  assert.equal(draftProblem({ ...ok, currency: "US" }), "errCurrency");
  assert.equal(draftProblem({ ...ok, basis: "" }), "errBasis");
  assert.equal(draftProblem({ ...ok, note: "n".repeat(501) }), "errNote");
});

test("draftChanged and saveBody", () => {
  assert.equal(draftChanged(ITEM, draftOf(ITEM)), false);
  assert.equal(draftChanged(ITEM, { ...draftOf(ITEM), amount: "1,500" }), true);
  assert.equal(draftChanged(SUGGESTED, draftOf(SUGGESTED)), true);
  assert.deepEqual(saveBody(ITEM, { amount: "1,500", currency: "usd", basis: "from", note: " " }), {
    amount: "1500", currency: "USD", basis: "from", note: null, expected_updated_at: SAVED.updated_at });
  assert.equal("expected_updated_at" in saveBody(SUGGESTED, draftOf(SUGGESTED)), false);
});

test("canAccept on suggestions, unconfirmed parses and prices to review only", () => {
  assert.equal(canAccept(SUGGESTED), true);
  assert.equal(canAccept({ ...ITEM, status: "parsed" }), true);
  assert.equal(canAccept({ ...ITEM, status: "needs_review" }), true);
  assert.equal(canAccept(ITEM), false);
  assert.equal(canAccept({ ...ITEM, status: "missing", saved: null }), false);
});

test("linesToShow keeps errors first, then the changes", () => {
  const rows = [
    { line: 2, source_key: "a", action: "create" }, { line: 3, source_key: "b", action: "skip" },
    { line: 4, source_key: "c", action: "error", error: "x" }, { line: 5, source_key: "d", action: "unchanged" },
    { line: 6, source_key: "e", action: "update" },
  ];
  assert.deepEqual(linesToShow(rows).map((r) => r.line), [4, 2, 6]);
  assert.equal(linesToShow(rows, 1).length, 1);
});

test("base64 round trip and error text", () => {
  const bytes = new Uint8Array([80, 75, 3, 4, 0, 255]);
  assert.deepEqual(fromBase64(toBase64(bytes)), bytes);
  assert.equal(errorMessage("Tour not found", 404), "Tour not found");
  assert.equal(errorMessage([{ loc: ["body", "amount"], msg: "Field required" }], 422), "amount: Field required");
  assert.equal(errorMessage({ message: "2 line(s) have errors" }, 422), "2 line(s) have errors");
  assert.equal(errorMessage(null, 500), "Request failed (500)");
});
