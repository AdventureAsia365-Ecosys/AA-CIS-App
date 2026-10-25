// frontend/lib/supplyContent.test.mjs
// AA-835: unit tests for the supply content review helpers in supplyContent.ts.
// Run: npm run test:unit (node --test with --experimental-strip-types; supplyContent.ts holds only type-level TS).
//
// The publish rules mirror services/supply_content/review.py publish() (+ _photo_problems): the page must warn about
// exactly what the API would refuse with 409.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  bookingHref,
  detailHref,
  factRows,
  factText,
  formFrom,
  formProblems,
  isRepublish,
  listUrl,
  photoState,
  publishBlockers,
  supplyErrorText,
  versionBody,
} from "./supplyContent.ts";

const media = (id, extra = {}) => ({
  media_id: id, usage_rights: true, active: true, content_type: "image/jpeg", width: 1600, height: 1067,
  url_600: `https://cdn.example.test/${id}-600.webp`, url_1600: `https://cdn.example.test/${id}-1600.webp`,
  copied_at: "2026-10-24T00:00:00Z", ...extra,
});
const entity = (extra = {}) => ({
  id: "e1", source_system: "aa_booking", source_id: "s1", entity_type: "hotel", service_type: null, title: "H",
  country_code: "VN", facts: {}, applied_supply_version: 3, active: true, content_status: "in_review",
  published_version_id: null, created_at: null, updated_at: null, ...extra,
});
const version = (extra = {}) => ({
  id: "v1", content_version: 2, supply_version: 3, origin: "llm", description: "D.", highlights: ["a"],
  good_to_know: ["g"], photos: [{ media_id: "m1", caption: "Pool", alt: "A pool" }], status: "draft",
  based_on_version_id: null, llm_job_id: null, created_by: null, created_at: null, reviewed_by: null,
  reviewed_at: null, review_note: null, ...extra,
});

test("the list URL carries the filters and the server page", () => {
  assert.equal(listUrl({ status: "", type: "" }, 0, 25), "/api/admin/supply-content?limit=25&offset=0");
  assert.equal(listUrl({ status: "stale", type: "hotel" }, 2, 50),
    "/api/admin/supply-content?status=stale&entity_type=hotel&limit=50&offset=100");
  assert.equal(detailHref("abc-1"), "/admin/supply-content/abc-1");
});

test("photo state follows the publish rule", () => {
  assert.equal(photoState(media("m1")), "ready");
  assert.equal(photoState(media("m1", { url_1600: null })), "not_copied");
  assert.equal(photoState(media("m1", { usage_rights: false })), "no_rights");
  assert.equal(photoState(media("m1", { active: false, usage_rights: false })), "removed");
  assert.equal(photoState(undefined), "unknown");
});

test("a clean draft has no publish blocker", () => {
  assert.deepEqual(publishBlockers(entity(), version(), [media("m1")]), []);
});

test("outdated facts, an inactive entity and an uncleared photo block publish", () => {
  const b = publishBlockers(entity({ active: false, applied_supply_version: 4 }), version(),
    [media("m1", { usage_rights: false })]);
  assert.equal(b.length, 3);
  assert.match(b[0], /inactive/);
  assert.match(b[1], /facts v3; Booking is at v4/);
  assert.match(b[2], /1 photo is not cleared/);
  // a photo that is not among the entity's media counts too
  assert.match(publishBlockers(entity(), version(), [])[0], /1 photo is not cleared/);
});

test("only the published approved version can be published again, after an unpublish or stale", () => {
  const v = version({ status: "approved" });
  assert.equal(isRepublish(entity({ published_version_id: "v1", content_status: "unpublished" }), v), true);
  assert.equal(isRepublish(entity({ published_version_id: "v1", content_status: "published" }), v), false);
  assert.match(publishBlockers(entity({ published_version_id: "v1", content_status: "published" }), v, [media("m1")])[0],
    /version is approved/);
  assert.deepEqual(publishBlockers(entity({ published_version_id: "v1", content_status: "stale" }), v, [media("m1")]), []);
  assert.match(publishBlockers(entity(), version({ status: "rejected" }), [media("m1")])[0], /rejected/);
});

test("a real stale entity cannot take the same version back: newer facts block the republish", () => {
  // pull.status_after_apply sets stale only after newer facts were applied, so the published version is outdated
  const e = entity({ published_version_id: "v1", content_status: "stale", applied_supply_version: 4 });
  const v = version({ status: "approved" });
  assert.equal(isRepublish(e, v), true);
  const b = publishBlockers(e, v, [media("m1")]);
  assert.equal(b.length, 1);
  assert.match(b[0], /facts v3; Booking is at v4/);
});

test("the editor copies the base version and keeps photo captions", () => {
  const base = version();
  const f = formFrom(base);
  assert.deepEqual(f, { description: "D.", highlights: "a", goodToKnow: "g", photoIds: ["m1"] });
  assert.deepEqual(formFrom(null), { description: "", highlights: "", goodToKnow: "", photoIds: [] });
  const body = versionBody({ ...f, highlights: " x  y \n\n b ", photoIds: ["m1", "m2"] }, base);
  assert.deepEqual(body, {
    description: "D.", highlights: ["x y", "b"], good_to_know: ["g"],
    photos: [{ media_id: "m1", caption: "Pool", alt: "A pool" }, { media_id: "m2", caption: null, alt: null }],
    based_on_version_id: "v1",
  });
  assert.equal(versionBody(formFrom(null), null).based_on_version_id, null);
});

test("form problems mirror the API's 422 rules", () => {
  assert.deepEqual(formProblems({ description: "ok", highlights: "", goodToKnow: "", photoIds: [] }), []);
  const p = formProblems({ description: " ", highlights: "1\n2\n3\n4\n5\n6", goodToKnow: "x".repeat(161),
    photoIds: [] });
  assert.equal(p.length, 3);
  assert.match(p[0], /empty/);
  assert.match(formProblems({ description: "x".repeat(2001), highlights: "", goodToKnow: "a\nb\nc\nd", photoIds: [] }).join(" "),
    /2000 characters.*At most 3/);
});

test("API errors read as one message", () => {
  assert.deepEqual(supplyErrorText(409, { detail: { error: "photo_rights", message: "a photo has no usage rights", media_ids: ["a", "b"] } }),
    { code: "photo_rights", message: "a photo has no usage rights (2 photos)" });
  assert.deepEqual(supplyErrorText(409, { detail: { error: "outdated", message: "the facts changed" } }),
    { code: "outdated", message: "the facts changed" });
  assert.deepEqual(supplyErrorText(422, { detail: [{ loc: ["body", "highlights"], msg: "too long" }] }),
    { code: "invalid", message: "highlights: too long" });
  assert.deepEqual(supplyErrorText(403, { detail: "Role 'viewer' lacks content.write" }),
    { code: null, message: "Role 'viewer' lacks content.write" });
  assert.deepEqual(supplyErrorText(403, null), { code: "forbidden", message: "Your role cannot do this." });
  assert.deepEqual(supplyErrorText(500, null), { code: null, message: "Request failed (500)" });
});

test("facts and links render plainly", () => {
  assert.equal(factText(null), "—");
  assert.equal(factText(["a", "b"]), "a, b");
  assert.equal(factText({ a: 1 }), '{"a":1}');
  assert.equal(factText("x".repeat(5), 3), "xxx…");
  assert.deepEqual(factRows({ zeta: 1, media: [], name: "N", alpha: 2, city: "Hue" }).map((r) => r[0]),
    ["name", "city", "alpha", "zeta"]);
  assert.equal(bookingHref("https://booking.example.test/", "/hotels?hotel=s1"), "https://booking.example.test/hotels?hotel=s1");
  assert.equal(bookingHref("https://booking.example.test", "services/s1"), "https://booking.example.test/services/s1");
});
