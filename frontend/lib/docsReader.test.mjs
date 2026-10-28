// frontend/lib/docsReader.test.mjs
// AA-898: unit tests for the docs reader helpers in docsReader.ts (invented paths; this repo ships no docs).
// Run: npm run test:unit (node --test with --experimental-strip-types).

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  ALL,
  DEFAULT_DOC_LANG,
  Slugger,
  audienceOptions,
  groupBySection,
  listedPages,
  matchesFilter,
  normalizePath,
  originalOf,
  parseDocLang,
  pickLanguage,
  outline,
  resolveLink,
  sectionOf,
  slugify,
  tableOfContents,
  typeOptions,
} from "./docsReader.ts";

const page = (path, over = {}) => ({
  repo: "AA-Ecosys", path, id: "OPS-01", title: "Runbook", description: null, type: "how-to", status: "active",
  audience: ["engineer"], owner: "nghiep", last_reviewed: "2026-10-28", generated: false, access: "staff", bytes: 10,
  ...over,
});

const published = new Set(["docs/README.md", "docs/ops/README.md", "docs/ops/runbooks/rollback.md", "docs/adr/0001-x.md"]);

test("filters by type, audience and query", () => {
  const p = page("docs/ops/runbooks/rollback.md", { title: "Roll back", id: "OPS-02", audience: ["owner", "devops"] });
  const f = (o) => matchesFilter(p, { query: "", type: ALL, audience: ALL, ...o });
  assert.equal(f({}), true);
  assert.equal(f({ type: "how-to" }), true);
  assert.equal(f({ type: "reference" }), false);
  assert.equal(f({ audience: "owner" }), true);
  assert.equal(f({ audience: "pm" }), false);
  assert.equal(f({ query: "roll" }), true);
  assert.equal(f({ query: "ops-02" }), true);
  assert.equal(f({ query: "runbooks/" }), true);
  assert.equal(f({ query: "nothing" }), false);
});

test("options keep the known order and add unknown values", () => {
  const pages = [page("a.md", { type: "reference" }), page("b.md", { type: "tutorial", audience: ["pm", "zz"] }),
    page("c.md", { type: "weird" })];
  assert.deepEqual(typeOptions(pages), ["tutorial", "reference", "weird"]);
  assert.deepEqual(audienceOptions(pages), ["engineer", "pm", "zz"]);
});

test("sections group pages by folder, top-level docs first", () => {
  assert.equal(sectionOf("docs/README.md"), "docs");
  assert.equal(sectionOf("docs/ops/runbooks/x.md"), "ops/runbooks");
  const groups = groupBySection([page("docs/ops/b.md"), page("docs/glossary.md"), page("docs/adr/0001-x.md")]);
  assert.deepEqual(groups.map((g) => g.section), ["docs", "adr", "ops"]);
});

test("normalizePath", () => {
  assert.equal(normalizePath("docs/ops/../adr/./0001-x.md"), "docs/adr/0001-x.md");
  assert.equal(normalizePath("../x.md"), null);
});

test("resolveLink: published pages open in the reader", () => {
  const from = "docs/ops/README.md";
  assert.deepEqual(resolveLink("runbooks/rollback.md", from, published), { kind: "page", path: "docs/ops/runbooks/rollback.md", hash: "" });
  assert.deepEqual(resolveLink("../adr/0001-x.md#status", from, published), { kind: "page", path: "docs/adr/0001-x.md", hash: "status" });
  assert.deepEqual(resolveLink("../README.md", from, published), { kind: "page", path: "docs/README.md", hash: "" });
  assert.deepEqual(resolveLink("./", "docs/ops/runbooks/rollback.md", published).kind, "none");
  assert.deepEqual(resolveLink("../", "docs/ops/runbooks/rollback.md", published), { kind: "page", path: "docs/ops/README.md", hash: "" });
  assert.deepEqual(resolveLink("/docs/README.md", from, published), { kind: "page", path: "docs/README.md", hash: "" });
});

test("resolveLink: anchors, external links and everything else", () => {
  const from = "docs/ops/README.md";
  assert.deepEqual(resolveLink("#rollback", from, published), { kind: "anchor", hash: "rollback" });
  assert.deepEqual(resolveLink("README.md#x", from, published), { kind: "anchor", hash: "x" });
  assert.deepEqual(resolveLink("https://example.com/a", from, published), { kind: "external", href: "https://example.com/a" });
  assert.deepEqual(resolveLink("mailto:a@example.com", from, published).kind, "external");
  for (const bad of ["http://example.com", "javascript:alert(1)", "data:text/html,x", "//evil.example", "vbscript:x"]) {
    assert.equal(resolveLink(bad, from, published).kind, "none", bad);
  }
  assert.equal(resolveLink("../security/baseline.md", from, published).kind, "none", "an unpublished page is text");
  assert.equal(resolveLink("../../../../etc/passwd", from, published).kind, "none");
  assert.equal(resolveLink("%E0%A4%A", from, published).kind, "none");
  assert.equal(resolveLink(undefined, from, published).kind, "none");
});

test("slugs follow GitHub and stay unique", () => {
  assert.equal(slugify("4.3 Where docs live"), "43-where-docs-live");
  assert.equal(slugify("Tóm tắt (VI)"), "tóm-tắt-vi");
  const s = new Slugger();
  assert.deepEqual([s.slug("Rollback"), s.slug("Rollback"), s.slug("Rollback")], ["rollback", "rollback-1", "rollback-2"]);
});

test("outline and table of contents skip code fences and use line numbers", () => {
  const md = ["# Title", "", "## Step one", "```bash", "## not a heading", "```", "### Detail `x`", "## Step one",
    "#### Deep"].join("\n");
  assert.deepEqual(outline(md).map((h) => [h.line, h.level, h.slug]),
    [[1, 1, "title"], [3, 2, "step-one"], [7, 3, "detail-x"], [8, 2, "step-one-1"], [9, 4, "deep"]]);
  assert.deepEqual(tableOfContents(md).map((t) => t.slug), ["step-one", "detail-x", "step-one-1"]);
});

// ── languages (EN default, VI when a translation is published) ─────────────────────────────────

const en = page("docs/guides/ceo.md", {
  lang: "en", translation_of: null, translations: [{ lang: "vi", path: "docs/guides/ceo.vi.md", title: "CEO (VI)" }],
});
const vi = page("docs/guides/ceo.vi.md", { lang: "vi", translation_of: "docs/guides/ceo.md", translations: [] });
const solo = page("docs/ops/runbook.md", { lang: "en", translation_of: null, translations: [] });
const orphanVi = page("docs/ops/orphan.vi.md", { lang: "vi", translation_of: null, translations: [] });

test("the list shows originals only", () => {
  assert.deepEqual(listedPages([en, vi, solo, orphanVi]).map((p) => p.path), ["docs/guides/ceo.md", "docs/ops/runbook.md"]);
  // an older API answer without the fields lists everything
  assert.equal(listedPages([page("docs/a.md")]).length, 1);
});

test("pickLanguage: translation, no translation, English default", () => {
  assert.deepEqual(pickLanguage(en, "vi"), { path: "docs/guides/ceo.vi.md", lang: "vi", available: ["en", "vi"], missing: false });
  assert.deepEqual(pickLanguage(en, "en"), { path: "docs/guides/ceo.md", lang: "en", available: ["en", "vi"], missing: false });
  assert.deepEqual(pickLanguage(solo, "vi"), { path: "docs/ops/runbook.md", lang: "en", available: ["en"], missing: true });
  assert.deepEqual(pickLanguage(page("docs/b.md"), "vi").missing, true);   // fields absent: treated as English
});

test("originalOf maps a translation path back to its original", () => {
  const by = new Map([en, vi, solo].map((p) => [p.path, p]));
  assert.equal(originalOf("docs/guides/ceo.vi.md", by)?.path, "docs/guides/ceo.md");
  assert.equal(originalOf("docs/guides/ceo.md", by)?.path, "docs/guides/ceo.md");
  assert.equal(originalOf("docs/missing.md", by), undefined);
});

test("parseDocLang falls back to English", () => {
  assert.equal(DEFAULT_DOC_LANG, "en");
  assert.equal(parseDocLang("vi"), "vi");
  for (const v of [null, undefined, "", "VI", "fr", 1]) assert.equal(parseDocLang(v), "en");
});
