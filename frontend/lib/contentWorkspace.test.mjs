// frontend/lib/contentWorkspace.test.mjs
// AA-1148 — the content workspace helpers (lib/contentWorkspace.ts) and its page rules. Checks:
//   1. a stored tour round-trips through the form and the API body (numbers as numbers, blanks as null);
//   2. list items split on commas / semicolons / lines and are added once each, case-blind;
//   3. day cards move (clamped) and duplicate as copies;
//   4. the unlinked report URL carries its filters;
//   5. the pages: every content reader opens them (the viewer too, read-only), the tenant desk roles do not; the
//      editor's write controls follow content.write and "link every suggestion" content.publish;
//   6. the tour type choices come from the options (admin data) and keep a type the tour holds that was removed;
//   7. the notes above the editor (sheet tour, reader role, live facts, two-person read-only facts).
// Run: npm run test:unit.

import assert from "node:assert/strict";
import { test } from "node:test";
import {
  addItems, bodyOf, duplicateDay, emptyDay, emptyForm, formKey, formOf, missingItems, moveDay, splitItems,
  noticeKeys, savedTypeLabel, stateTone, tourHref, tourTypeChoices, unlinkedUrl,
} from "./contentWorkspace.ts";
import { can, canSeePage } from "./rbac.ts";

const STORED = {
  name: "Zephyr Delta", summary: "Slow days.", nights: 2, country: "VN", supplier_id: "s-1",
  days: [{ title: "Arrive", places: ["Quillmoor"], meals: ["lunch"], overnight: "Lodge", description: "Boat." }],
  inclusions: ["Transport"], exclusions: [], group_min: 2, group_max: null, tour_type: "private",
  guide_languages: ["English"], best_time_to_go: "", saved_at: "2026-11-24T00:00:00+00:00",
};

test("1. a stored tour round-trips through the form and the body", () => {
  const form = formOf(STORED);
  assert.equal(form.nights, "2");
  assert.equal(form.group_max, "");
  const body = bodyOf(form);
  assert.equal(body.nights, 2);
  assert.equal(body.group_min, 2);
  assert.equal(body.group_max, null);
  assert.equal(body.country, "VN");
  assert.equal(body.tour_type, "private");
  assert.deepEqual(body.days, STORED.days);
  const blank = bodyOf(emptyForm());
  assert.equal(blank.country, null);
  assert.equal(blank.supplier_id, null);
  assert.equal(blank.nights, null);
  assert.equal(bodyOf({ ...form, nights: "2x" }).nights, null);
  assert.notEqual(formKey(form), formKey({ ...form, name: "Other" }));
  form.days[0].places.push("x");
  assert.deepEqual(STORED.days[0].places, ["Quillmoor"], "formOf copies the lists");
});

test("2. list items split and are added once each", () => {
  assert.deepEqual(splitItems(" Water, guide;\nTickets ,, "), ["Water", "guide", "Tickets"]);
  assert.deepEqual(addItems(["Water"], ["water", "Guide", "guide"]), ["Water", "Guide"]);
});

test("3. day cards move and duplicate", () => {
  const days = ["a", "b", "c"].map((title) => ({ ...emptyDay(), title }));
  assert.deepEqual(moveDay(days, 0, 2).map((d) => d.title), ["b", "c", "a"]);
  assert.deepEqual(moveDay(days, 2, -5).map((d) => d.title), ["c", "a", "b"]);
  assert.equal(moveDay(days, 1, 1), days);
  const dup = duplicateDay(days, 1);
  assert.deepEqual(dup.map((d) => d.title), ["a", "b", "b", "c"]);
  assert.notEqual(dup[1].places, dup[2].places);
});

test("4. the unlinked report URL and the page links", () => {
  assert.equal(unlinkedUrl({ limit: 50, offset: 0 }), "/api/admin/content-workspace/supplier-links?limit=50&offset=0");
  assert.equal(unlinkedUrl({ country: "VN", q: " mekong ", onlySuggested: true, limit: 20, offset: 40 }),
    "/api/admin/content-workspace/supplier-links?country=VN&q=mekong&only_suggested=true&limit=20&offset=40");
  assert.equal(tourHref("a b"), "/admin/content-workspace/a%20b");
  assert.equal(stateTone("live"), "success");
  assert.equal(stateTone("draft"), "info");
  assert.deepEqual(missingItems([{ key: "a", ok: false, required: true }, { key: "b", ok: false, required: false },
    { key: "c", ok: true, required: true }]).map((i) => i.key), ["a"]);
});

test("5. who opens the pages and who writes", () => {
  for (const path of ["/admin/content-workspace", "/admin/content-workspace/new",
    "/admin/content-workspace/supplier-links", "/admin/content-workspace/x"]) {
    for (const role of ["admin", "content_lead", "content", "viewer"]) assert.ok(canSeePage(role, path), `${role} ${path}`);
    for (const role of ["customer_success", "support", "finance"]) assert.ok(!canSeePage(role, path), `${role} ${path}`);
  }
  assert.ok(can("content", "content.write"));
  assert.ok(!can("viewer", "content.write"));
  assert.ok(!can("content", "content.publish"));
  assert.ok(can("content_lead", "content.publish"));
});

test("6. tour type choices are the options in force, plus the removed type a tour still holds", () => {
  const opts = [{ code: "zz_small", label: "Zz small group", label_vi: "Zz nhóm nhỏ" },
    { code: "zz_self", label: "Zz self-guided", label_vi: "" }];
  assert.deepEqual(tourTypeChoices(opts, "", null, "en"), [{ value: "zz_small", label: "Zz small group" },
    { value: "zz_self", label: "Zz self-guided" }]);
  assert.deepEqual(tourTypeChoices(opts, "zz_small", "Zz small group", "vi").map((o) => o.label),
    ["Zz nhóm nhỏ", "Zz self-guided"]);
  const kept = tourTypeChoices(opts, "zz_gone", "Zz gone type", "en");
  assert.deepEqual(kept.at(-1), { value: "zz_gone", label: "Zz gone type" });
  assert.equal(tourTypeChoices([], "zz_gone", null, "en")[0].label, "zz_gone");
});

test("7. the notes above the editor", () => {
  const live = { entry_origin: "manual", reason_required: true, editable: true, facts_two_person: false,
    facts: { tour_type_label: "Zz small group" } };
  assert.deepEqual(noticeKeys(live, true), ["liveNote"]);
  assert.deepEqual(noticeKeys({ ...live, editable: false, facts_two_person: true }, true), ["factsTwoPerson"]);
  assert.deepEqual(noticeKeys(live, false), ["readOnlyRole"]);
  assert.deepEqual(noticeKeys({ ...live, entry_origin: "sheet", reason_required: false, editable: false }, false),
    ["readOnlySheet", "readOnlyRole"]);
  assert.deepEqual(noticeKeys(null, true), []);
  assert.equal(savedTypeLabel(live), "Zz small group");
  assert.equal(savedTypeLabel(null), null);
});
