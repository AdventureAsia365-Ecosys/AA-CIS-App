// frontend/lib/docsToc.test.mjs
// AA-1133 follow-up (S284 live check): the docs reader scroll-spy pick (lib/docsToc.ts). The side table of contents
// stayed on the previous heading after a click; these pin the pick from heading tops, the "went to" heading, and the
// #doc-<slug> address form. Invented slugs only.
// Run: npm run test:unit (node --test with --experimental-strip-types).

import assert from "node:assert/strict";
import { test } from "node:test";

import { ACTIVE_LINE_PX, PIN_SLACK_PX, addressHeading, headingHash, pickActiveIndex, slugFromHash } from "./docsToc.ts";

const VIEWPORT = 900;
const INF = Number.POSITIVE_INFINITY;

test("pickActiveIndex: the last heading whose top passed the line", () => {
  assert.equal(pickActiveIndex([-400, -20, 60, 300, 700], { viewport: VIEWPORT }), 2);
  assert.equal(pickActiveIndex([-400, -20, 200, 300], { viewport: VIEWPORT }), 1);
});

test("pickActiveIndex: a heading scrolled to its scroll margin (16 px) is the active one, not the one before", () => {
  // The live bug: after a click the heading sits at 16 px and the previous heading stayed marked.
  assert.equal(pickActiveIndex([-900, -300, 16, 500], { viewport: VIEWPORT }), 2);
});

test("pickActiveIndex: the line itself counts; above every heading the first one is active", () => {
  assert.equal(pickActiveIndex([ACTIVE_LINE_PX, 400], { viewport: VIEWPORT }), 0);
  assert.equal(pickActiveIndex([ACTIVE_LINE_PX - 1, ACTIVE_LINE_PX], { viewport: VIEWPORT }), 1);
  assert.equal(pickActiveIndex([300, 600], { viewport: VIEWPORT }), 0);
  assert.equal(pickActiveIndex([], { viewport: VIEWPORT }), 0);
});

test("pickActiveIndex: a heading missing from the page (Infinity) is never picked by position", () => {
  assert.equal(pickActiveIndex([-100, INF, 40], { viewport: VIEWPORT }), 2);
  assert.equal(pickActiveIndex([-100, INF, 400], { viewport: VIEWPORT }), 0);
});

test("pickActiveIndex: a custom line", () => {
  assert.equal(pickActiveIndex([-10, 150, 400], { viewport: VIEWPORT, line: 200 }), 1);
});

test("pickActiveIndex: the heading the reader went to stays active while it is on screen", () => {
  // A short last section: the page cannot scroll it up to the line, the click still marks it.
  assert.equal(pickActiveIndex([-800, -200, 40, 420, 610], { viewport: VIEWPORT, pinned: 4 }), 4);
  assert.equal(pickActiveIndex([-800, -200, 40, 420, -PIN_SLACK_PX], { viewport: VIEWPORT, pinned: 4 }), 4);
});

test("pickActiveIndex: a pinned heading off screen, missing or out of range falls back to the position", () => {
  assert.equal(pickActiveIndex([-800, -200, 40, 420, VIEWPORT], { viewport: VIEWPORT, pinned: 4 }), 2);
  assert.equal(pickActiveIndex([-800, -PIN_SLACK_PX - 1, 40, 420], { viewport: VIEWPORT, pinned: 1 }), 2);
  assert.equal(pickActiveIndex([-800, INF, 40], { viewport: VIEWPORT, pinned: 1 }), 2);
  assert.equal(pickActiveIndex([-800, -200, 40], { viewport: VIEWPORT, pinned: 7 }), 2);
  assert.equal(pickActiveIndex([-800, -200, 40], { viewport: VIEWPORT, pinned: -1 }), 2);
});

test("slugFromHash: reads #doc-<slug>, encoded or not; anything else names no heading", () => {
  assert.equal(slugFromHash("#doc-opening-hours", "doc-"), "opening-hours");
  assert.equal(slugFromHash("doc-opening-hours", "doc-"), "opening-hours");
  assert.equal(slugFromHash("#doc-%C4%91%C3%A8n-l%E1%BB%93ng", "doc-"), "đèn-lồng");
  assert.equal(slugFromHash("#doc-đèn-lồng", "doc-"), "đèn-lồng");
  assert.equal(slugFromHash("#doc-%E0%A4%A", "doc-"), "%E0%A4%A");
  assert.equal(slugFromHash("#top", "doc-"), "");
  assert.equal(slugFromHash("", "doc-"), "");
});

test("headingHash: the shareable form, round-trips through slugFromHash", () => {
  assert.equal(headingHash("opening-hours", "doc-"), "#doc-opening-hours");
  assert.equal(headingHash("", "doc-"), "");
  for (const slug of ["lantern-stalls", "đèn-lồng", "a&b"]) {
    assert.equal(slugFromHash(headingHash(slug, "doc-"), "doc-"), slug);
  }
});

test("addressHeading: a shared ?path=…#doc-<slug> address seeds the jump; no location or another hash does not", () => {
  // The review finding: a fresh load of a shared link stayed at the top (the jump only came from in-app clicks).
  assert.equal(addressHeading({ hash: "#doc-river-crossing" }, "doc-"), "river-crossing");
  assert.equal(addressHeading({ hash: headingHash("lantern café", "doc-") }, "doc-"), "lantern café");
  assert.equal(addressHeading({ hash: "" }, "doc-"), "");
  assert.equal(addressHeading({ hash: "#top" }, "doc-"), "");
  assert.equal(addressHeading(undefined, "doc-"), "");
  assert.equal(addressHeading(null, "doc-"), "");
});
