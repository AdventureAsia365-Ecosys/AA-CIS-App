// frontend/lib/aaContent.test.mjs
// AA-977 (S3): unit tests for the AA content editor helpers in aaContent.ts.
// Run: npm run test:unit (node --test with --experimental-strip-types; aaContent.ts holds only type-level TS).
//
// The visa cases are the ones services/aa_content/validate.py is tested with (tests/unit/test_aa977_*), so the
// editor's warning agrees with the 422 the backend answers at publish.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  aaErrorText,
  appliesFromForm,
  baselineVersion,
  formFromVersion,
  formProblems,
  isDirty,
  isGroupKey,
  listUrl,
  parseTours,
  pendingDraftLabel,
  sections,
  stateBadge,
  suggestGroupKey,
  visaSectionsWithoutLink,
} from "./aaContent.ts";

const v = (version, status, extra = {}) => ({
  version, status, title: `t${version}`, body_markdown: `b${version}`, applies_to: { country_codes: ["VN"] },
  content_hash: status === "published" ? "sha256:" + "0".repeat(64) : null, published_at: null, published_by: null,
  created_at: null, created_by: null, updated_at: null, ...extra,
});

test("the list URL carries the filters and the server page", () => {
  assert.equal(listUrl({ kind: "", locale: "", status: "", country: "" }, 0, 25), "/api/admin/aa-content?limit=25&offset=0");
  assert.equal(
    listUrl({ kind: "prep_pack", locale: "vi", status: "draft", country: "VN" }, 2, 50),
    "/api/admin/aa-content?kind=prep_pack&locale=vi&status=draft&country=VN&limit=50&offset=100",
  );
  assert.equal(listUrl({ kind: "", locale: "", status: "", country: "" }, -1, 25), "/api/admin/aa-content?limit=25&offset=0");
});

test("state badges: draft, published vN, retired, and a waiting draft", () => {
  assert.deepEqual(stateBadge({ state: "draft", published_version: null }), { label: "Draft", tone: "warning" });
  assert.deepEqual(stateBadge({ state: "published", published_version: 3 }), { label: "Published v3", tone: "success" });
  assert.equal(stateBadge({ state: "retired", published_version: 2 }).label, "Retired");
  assert.equal(pendingDraftLabel({ state: "published", draft_version: 4, published_version: 3 }), "Draft v4 waiting");
  assert.equal(pendingDraftLabel({ state: "draft", draft_version: 1, published_version: null }), null);
  assert.equal(pendingDraftLabel({ state: "published", draft_version: null, published_version: 3 }), null);
});

test("the editor opens on the draft, else on the newest published version", () => {
  const withDraft = { versions: [v(3, "draft"), v(2, "published"), v(1, "published")] };
  assert.equal(baselineVersion(withDraft).version, 3);
  const published = { versions: [v(2, "published"), v(1, "published")] };
  assert.equal(baselineVersion(published).version, 2);
  assert.equal(baselineVersion({ versions: [] }), null);
  assert.deepEqual(formFromVersion(null), { title: "", body_markdown: "", country_codes: [], tours_text: "" });
  const f = formFromVersion(v(1, "draft", { applies_to: { country_codes: ["TH"], tours: [{ source_system: "cis", source_id: "T-1" }] } }));
  assert.deepEqual(f, { title: "t1", body_markdown: "b1", country_codes: ["TH"], tours_text: "T-1" });
});

test("tours: one id per line, trimmed, duplicates dropped; applies_to sends only what is set", () => {
  assert.deepEqual(parseTours(" T-1 \n\nT-2\r\nT-1\n"), [
    { source_system: "cis", source_id: "T-1" }, { source_system: "cis", source_id: "T-2" },
  ]);
  assert.deepEqual(appliesFromForm({ country_codes: [], tours_text: "  " }), {});
  assert.deepEqual(appliesFromForm({ country_codes: ["VN", "VN", "LA"], tours_text: "T-9" }), {
    country_codes: ["VN", "LA"], tours: [{ source_system: "cis", source_id: "T-9" }],
  });
});

test("dirty tracking ignores whitespace-only tour edits", () => {
  const base = { title: "a", body_markdown: "b", country_codes: ["VN"], tours_text: "T-1" };
  assert.equal(isDirty({ ...base }, base), false);
  assert.equal(isDirty({ ...base, tours_text: " T-1 \n" }, base), false);
  assert.equal(isDirty({ ...base, title: "a!" }, base), true);
  assert.equal(isDirty({ ...base, country_codes: ["VN", "LA"] }, base), true);
  assert.equal(isDirty({ ...base, country_codes: [] }, base), true);
});

test("form problems mirror the title / body / tour limits", () => {
  const ok = { title: "x", body_markdown: "", country_codes: [], tours_text: "" };
  assert.deepEqual(formProblems(ok), {});
  assert.ok(formProblems({ ...ok, title: "  " }).title);
  assert.ok(formProblems({ ...ok, title: "x".repeat(301) }).title);
  assert.ok(formProblems({ ...ok, body_markdown: "x".repeat(20001) }).body_markdown);
  assert.ok(formProblems({ ...ok, tours_text: "x".repeat(129) })["applies_to.tours"]);
});

test("sections join a heading-only block to the paragraph after it", () => {
  assert.deepEqual(sections("## Visa\n\nApply online.\n\n## Money\n\nCash."), ["## Visa\nApply online.", "## Money\nCash."]);
  assert.deepEqual(sections("## Only a heading"), ["## Only a heading"]);
});

test("the visa rule: a visa section needs an https link", () => {
  assert.equal(visaSectionsWithoutLink("## Visa\n\nMost visitors need an e-visa.").length, 1);
  assert.deepEqual(visaSectionsWithoutLink("## Visa\n\nApply at https://evisa.gov.vn before you fly."), []);
  // http:// is not an official link
  assert.equal(visaSectionsWithoutLink("Visa: see http://example.org").length, 1);
  // eVisa / e-visas / Vietnamese, also when typed decomposed (NFD)
  assert.equal(visaSectionsWithoutLink("Get your eVisa early.").length, 1);
  assert.equal(visaSectionsWithoutLink("Two e-visas are needed.").length, 1);
  assert.equal(visaSectionsWithoutLink("Bạn cần thị thực điện tử.").length, 1);
  assert.equal(visaSectionsWithoutLink("Bạn cần thị thực điện tử.".normalize("NFD")).length, 1);
  // a payment card is not a visa
  assert.deepEqual(visaSectionsWithoutLink("Visa card and Mastercard are accepted."), []);
  assert.deepEqual(visaSectionsWithoutLink("Pay by Visa/Mastercard or cash."), []);
  assert.deepEqual(visaSectionsWithoutLink("Visa debit works at most ATMs."), []);
  // card + a real visa mention in one section still needs the link
  assert.equal(visaSectionsWithoutLink("Visa card accepted. You also need a visa on arrival.").length, 1);
  // a word that only contains "visa" is not a mention
  assert.deepEqual(visaSectionsWithoutLink("Our advisory team (supervisa) helps."), []);
  // the first 80 characters are reported
  assert.equal(visaSectionsWithoutLink(`Visa ${"x".repeat(200)}`)[0].length, 80);
});

test("group keys: the validate.py pattern and a slug suggestion", () => {
  assert.equal(isGroupKey("prep-pack-vn"), true);
  assert.equal(isGroupKey("-bad"), false);
  assert.equal(isGroupKey("a"), false);
  assert.equal(isGroupKey("Upper"), false);
  assert.equal(suggestGroupKey("Before you go: Việt Nam (2027)"), "before-you-go-viet-nam-2027");
  assert.equal(suggestGroupKey("Đà Lạt"), "da-lat");
  assert.equal(suggestGroupKey("x".repeat(100)).length, 80);
  assert.equal(isGroupKey(suggestGroupKey("Welcome email")), true);
});

test("error text: stale, role, storage missing, field detail", () => {
  assert.match(aaErrorText(409, "Someone else saved", "stale"), /Reload/);
  assert.match(aaErrorText(403, "Role 'content' lacks master.admin", null), /admin only/);
  assert.equal(aaErrorText(403, "Only an admin can do this", null), "Only an admin can do this");
  assert.match(aaErrorText(503, "x", "unavailable"), /not available/);
  assert.equal(aaErrorText(422, "Visa information must point…", "visa_needs_official_link"), "Visa information must point…");
  assert.match(aaErrorText(422, [{ loc: ["body", "title"] }], null), /field/);
  assert.match(aaErrorText(0, null, null), /Network/);
  assert.equal(aaErrorText(500, undefined, null), "Request failed (500).");
});
