// AA-1127 C4 (S283 UI audit): Tenant content QA layout. Content quality fits 1440 and 390, Content Trace uses the kit
// filters inside the content column, and a server-paged table has one search box (the page's), never the kit's.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const FRONTEND = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (p) => readFileSync(join(FRONTEND, p), "utf8");
const QA = ["quality", "trace", "review-log", "slate", "compliance"];

test("the QA sub-nav is a row above the content on every QA page (no 200 px column)", () => {
  const nav = read("app/admin/_components/SocialContentSubNav.tsx");
  assert.match(nav, /useMediaQuery\(SPLIT_QUERY\) \|\| !catalogue/);
  assert.match(nav, /useMediaQuery\(SPLIT_QUERY\) \|\| stack/);
  for (const page of QA) {
    const src = read(`app/admin/tenant-qa/${page}/page.tsx`);
    assert.match(src, /<SocialContentSplit gap=\{16\} stack>/, page);
  }
});

test("Content quality: gates in one bounded column, title capped, dates in the kit format, filters wrap", () => {
  const src = read("app/admin/tenant-qa/quality/page.tsx");
  assert.match(src, /header: t\("colGateResults"\)/);
  assert.doesNotMatch(src, /header: t\("colWarnings"\)/);
  assert.match(src, /maxWidth: 240, minWidth: 0/);
  assert.doesNotMatch(src, /fmtTime\(/);
  assert.match(src, /const filterRow: React\.CSSProperties = \{[^}]*minWidth: 0, maxWidth: "100%"/);
  const m = read("app/admin/tenant-qa/quality/messages.ts");
  assert.equal(m.match(/colGateResults:/g)?.length, 2, "English and Vietnamese");
});

test("the shared tenant picker never grows wider than its row", () => {
  const src = read("app/admin/_components/TenantSelect.tsx");
  assert.match(src, /inlineFieldStyle: React\.CSSProperties = \{[^}]*maxWidth: "min\(100%, 280px\)"/);
  assert.match(src, /inlineSearchStyle: React\.CSSProperties = \{[^}]*maxWidth: "100%"/);
});

test("Content Trace: kit FilterField and DateField, in the content column, opened by ?tenant=", () => {
  const src = read("app/admin/tenant-qa/trace/page.tsx");
  assert.doesNotMatch(src, /function FilterField\(/, "the page-local FilterField (no label element) is gone");
  assert.doesNotMatch(src, /type="date"/);
  assert.equal(src.match(/<DateField /g)?.length, 2);
  const header = src.slice(src.indexOf("<PageFrameHeader>"), src.indexOf("</PageFrameHeader>"));
  assert.doesNotMatch(header, /FilterField|TenantSelect|StatTile/, "filters and tiles live in the content column");
  const body = src.slice(src.indexOf('<SocialContentSubNav active="content_trace" />'));
  assert.ok(body.indexOf("<TraceFilters f={filters} />") > 0);
  assert.match(src, /function TraceFilters\(/);
  assert.match(src, /useSearchParams\(\)\.get\("tenant"\)/);
  assert.match(src, /<Suspense fallback=\{null\}><ContentTracePage \/><\/Suspense>/);
});

test("one search box on tables whose page has its own search", () => {
  const quality = read("app/admin/tenant-qa/quality/page.tsx");
  assert.equal(quality.match(/searchable=\{false\}/g)?.length, 2, "pieces and tour versions");
  assert.match(read("app/admin/places/cards/page.tsx"), /searchable=\{false\}/);
  assert.match(read("app/admin/tenants/assignments/Assignments.tsx"), /searchable=\{false\}/);
});
