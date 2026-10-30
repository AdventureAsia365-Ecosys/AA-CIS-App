// frontend/lib/writingMemoryLayout.test.mjs
// S260 F3 (AA-922): the Writing memory tabs stay inside the admin frame (`main`, the shell's own scroller) at 1440
// and 390. Live evidence found two traps; this guards the source against both. Run: npm run test:unit.
//
//   1. A tab whose root is `display: grid` must give it an explicit `minmax(0, 1fr)` track. The implicit track is
//      `auto`, which grows to the widest item's min-content width: the KPI table's (tableMinWidth plus nowrap
//      headers) made the Metrics tab 1,413 px wide in a 1,204 px frame, so the header actions and the last columns
//      sat off-screen. `minWidth: 0` on the grid itself does not help: it is the track, not the box, that grows.
//      A fragment root is fine (block children never grow past their parent).
//   2. An auto-fit / auto-fill track takes `minmax(min(100%, Npx), 1fr)`, never a bare `Npx` minimum, so a tile
//      never forces a column wider than a phone.
//   3. The summary tiles above the tabs wrap a long token (the "automatic mining is off (<env switch>)" hint stuck
//      10 px out of its tile at 390 and gave the frame a sideways scroll on every tab).

import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const DIR = fileURLToPath(new URL("../app/admin/writing-memory", import.meta.url));
const FILES = readdirSync(DIR).filter((f) => f.endsWith(".tsx"));

/** Source without comments, so a comment that quotes a style never satisfies or trips a check. */
function code(name) {
  return readFileSync(join(DIR, name), "utf8")
    .replace(/^\s*\/\/.*$/gm, "")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/(^|[^:"'`])\/\/.*$/gm, "$1");
}

/** Every tab component: `function XTab(` (in-file tabs) and `export default function XTab(`. */
function tabs() {
  const out = [];
  for (const f of FILES) {
    const src = code(f);
    for (const m of src.matchAll(/(?:export default )?function (\w+Tab)\s*\(/g)) out.push({ file: f, name: m[1], src, at: m.index });
  }
  return out;
}

/** The opening tag of the JSX a function returns at its top level: the last `return (` before the next top-level
 *  function (hooks and helpers inside the tab return earlier, so the last one is the render). */
function rootTag(src, at) {
  const next = src.slice(at + 1).search(/\n(?:export default )?function \w+/);
  const body = next === -1 ? src.slice(at) : src.slice(at, at + 1 + next);
  const returns = [...body.matchAll(/\n  return \(\s*\n\s*(<[^\n]*)/g)];
  assert.ok(returns.length, "no top-level `return (` found");
  return returns[returns.length - 1][1];
}

test("the guard finds the tabs it is meant to check", () => {
  const names = tabs().map((t) => t.name).sort();
  for (const n of ["CandidatesTab", "ExemplarsTab", "MetricsTab", "ReleasesTab", "SignalsTab"]) {
    assert.ok(names.includes(n), `${n} not found (renamed? update this guard)`);
  }
});

test("a tab with a grid root caps its track at the frame width (minmax(0, 1fr))", () => {
  const offenders = [];
  for (const { file, name, src, at } of tabs()) {
    const tag = rootTag(src, at);
    if (tag.startsWith("<>")) continue;
    if (/display: "grid"/.test(tag) && !/gridTemplateColumns: "minmax\(0, 1fr\)"/.test(tag)) {
      offenders.push(`${file} ${name}: ${tag.trim()}`);
    }
  }
  assert.deepEqual(offenders, [], offenders.join("\n"));
});

test("auto-fit / auto-fill tracks never force a column wider than the screen", () => {
  const offenders = [];
  for (const f of FILES) {
    for (const m of code(f).matchAll(/repeat\(auto-(?:fit|fill), minmax\((\d+)px/g)) offenders.push(`${f}: minmax(${m[1]}px, …)`);
  }
  assert.deepEqual(offenders, [], `${offenders.join("\n")}\nuse minmax(min(100%, Npx), 1fr)`);
});

test("the summary tiles wrap a long token instead of sticking out of the frame", () => {
  const src = code("WritingMemory.tsx");
  const stat = src.slice(src.indexOf("function Stat("), src.indexOf("function Pair("));
  assert.match(stat, /overflowWrap: "anywhere"/);
  assert.match(stat, /minWidth: "min\(150px, 100%\)"/);
});
