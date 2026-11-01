// frontend/lib/adminUiS263.test.mjs
// AA-1006 follow-up — the S263 production UI check (four findings). Source guards, so a later edit does not bring a
// finding back; the logic parts are tested in pageRange.test.mjs and contentTrace.test.mjs. Run: npm run test:unit.
//
//   F1 /admin/overview Alerts at 390: a long alert key ("tenant_ai_budget:<uuid>") and the date pushed the page frame
//      65 px wider than the phone → the key wraps anywhere, the row stacks on a phone, the date never breaks inside.
//   F2 kit DataTable toolbar ("Columns") flush against / clipped at the right edge: Master Content drew the table
//      full-bleed under an inset header, Upload put it flush in a `Section padded={false}` card → a consistent inset.
//   F3 /admin/tenant-activity Content Trace: "Total pieces 200" was the number of loaded rows, there was no pager, and
//      ten columns did not fit the pane at 1440 → server paging, the API's own counts, seven columns.
//   F4 Master Content numbered the previous page's rows with the next page's numbers during "Refreshing…" → the
//      range follows the rows on screen.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const APP = fileURLToPath(new URL("../app/admin/", import.meta.url));

/** Source without comments, so a comment that quotes a style never satisfies or trips a check. */
function code(rel) {
  return readFileSync(APP + rel, "utf8")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/\{\/\*[\s\S]*?\*\/\}/g, "")
    .replace(/^\s*\/\/.*$/gm, "")
    .replace(/(^|[^:"'`])\/\/.*$/gm, "$1");
}

// ── F1 ───────────────────────────────────────────────────────────────────────────────────────────────────────────

test("F1: the Overview renders its alerts through AlertList (no inline one-line row left)", () => {
  const page = code("overview/page.tsx");
  assert.match(page, /<AlertList alerts=\{data\.alerts\} \/>/);
  assert.doesNotMatch(page, /data\.alerts\.map\(/, "the old nowrap alert row is back in page.tsx");
});

test("F1: an alert row wraps a long key and stacks on a phone", () => {
  const src = code("overview/_components/AlertList.tsx");
  assert.match(src, /useIsPhone\(\)/);
  assert.match(src, /flexDirection: isPhone \? "column" : "row"/, "the row must stack on a phone");
  assert.match(src, /flexWrap: "wrap"/, "the desktop row must wrap instead of overflowing");
  // The key span: shrinkable below its longest token and wrapping anywhere.
  const key = src.slice(src.indexOf("a.entity_type") - 400, src.indexOf("a.entity_type"));
  assert.match(key, /minWidth: 0/);
  assert.match(key, /overflowWrap: "anywhere"/);
  // The date keeps its own line together.
  const date = src.slice(src.indexOf("formatDateTime(a.created_at)") - 200, src.indexOf("formatDateTime(a.created_at)"));
  assert.match(date, /whiteSpace: "nowrap"/);
});

// ── F2 ───────────────────────────────────────────────────────────────────────────────────────────────────────────

test("F2: the Master Content table sits at the header's inset (36 px, 16 px on a phone)", () => {
  const src = code("master-content/page.tsx");
  const at = src.indexOf('tableId="master-content-tours"');
  assert.ok(at > 0, "Master Content table not found (renamed? update this guard)");
  const before = src.slice(src.lastIndexOf("<div", src.lastIndexOf("<DataTable<MasterRow>", at)), at);
  assert.match(before, /padding: isPhone \? "12px 16px" : "14px 36px"/);
  // The sticky header above uses the same inset.
  assert.match(src, /padding: isPhone \? "10px 16px 8px" : "10px 36px 8px"/);
});

test("F2: every DataTable on the Upload page sits in the card inset (TableInset)", () => {
  const src = code("upload/page.tsx");
  assert.match(src, /function TableInset\(/);
  assert.match(src, /padding: "10px 20px 16px"/);
  // ReadyToursTable's own DataTable is wrapped where ReadyToursTable is used, so its body is skipped.
  const rtStart = src.indexOf("function ReadyToursTable(");
  const rtEnd = src.indexOf("\nfunction ", rtStart + 1);
  assert.ok(rtStart > 0 && rtEnd > rtStart, "ReadyToursTable not found (renamed? update this guard)");
  const lines = src.split("\n");
  let at = 0;
  let prev = "";
  let checked = 0;
  for (const [i, line] of lines.entries()) {
    const tag = line.trim();
    const start = at;
    at += line.length + 1;
    if ((tag.startsWith("<DataTable") || tag.startsWith("<ReadyToursTable")) && !(start > rtStart && start < rtEnd)) {
      assert.equal(prev, "<TableInset>", `line ${i + 1} (${tag.slice(0, 40)}) is not wrapped in <TableInset>`);
      checked++;
    }
    if (tag) prev = tag;
  }
  assert.ok(checked >= 5, `expected the five Upload tables, checked ${checked}`);
});

// ── F3 ───────────────────────────────────────────────────────────────────────────────────────────────────────────

test("F3: Content Trace pages on the server and counts from the API", () => {
  const src = code("tenant-activity/page.tsx");
  assert.match(src, /traceQuery\(filterQuery, pageIndex, pageSize\)/);
  assert.match(src, /serverPagination=\{paging\}/);
  assert.match(src, /traceStats\(answer\)/);
  assert.doesNotMatch(src, /total: list\.length/, "the header counts the loaded rows again");
  assert.doesNotMatch(src, /paginate=\{false\}/, "the table shows every loaded row with no pager again");
});

test("F3: Content Trace has seven columns, the text ones wrap with a readable minimum", () => {
  const src = code("tenant-activity/page.tsx");
  const cols = [...src.matchAll(/\bcol\("(\w+)"/g)].map((m) => m[1]);
  assert.deepEqual(cols, ["topic", "tenant", "channel", "angle", "status", "gate", "created_at"]);
  assert.match(src, /col\("topic"[^\n]*textBox\(150, 220\)/);
  assert.match(src, /col\("angle"[^\n]*textBox\(140, 200\)/);
});

// ── F4 ───────────────────────────────────────────────────────────────────────────────────────────────────────────

test("F4: Master Content numbers rows and shows the range of the page on screen", () => {
  const src = code("master-content/page.tsx");
  assert.match(src, /shownRange\(data\?\.pagination, \{ page, pageSize \}, tours\.length\)/);
  assert.match(src, /shown\.offset \+ row\.index \+ 1/);
  assert.match(src, /t\("range", shown\.from, shown\.to, serverTotal\)/);
  assert.match(src, /<Pagination page=\{shown\.page\}/);
  assert.doesNotMatch(src, /\(page - 1\) \* pageSize/, "a row number or range is computed from the requested page again");
});
