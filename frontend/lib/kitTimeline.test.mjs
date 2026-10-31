// frontend/lib/kitTimeline.test.mjs
// AA-804 (UI part 3) — the kit core TraceTimeline (app/_kit/core/TraceTimeline.tsx) and LatencyStrip
// (app/_kit/core/LatencyStrip.tsx). Run: npm run test:unit.
//
// Like kitButtons.test.mjs, the core TSX is transpiled with the TypeScript compiler into a temporary folder inside
// frontend/ and rendered with react-dom/server. Checks:
//   1. formatDuration and sinceStart (ms, s, m s, h m; unreadable values);
//   2. TraceTimeline: an ordered list with one row per event, offsets from the first event, label / title / detail /
//      amount / duration, a button per row with onSelect, the empty text, the scroll box;
//   3. latencyStats: nearest-rank p50 / p95 / max, untimed values left out;
//   4. LatencyStrip: one bar per call, an image whose label is the summary line, the empty summary.

import assert from "node:assert/strict";
import { mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { after, before, test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";
import { createRequire } from "node:module";

const FRONTEND = fileURLToPath(new URL("..", import.meta.url));
const CORE = join(FRONTEND, "app", "_kit", "core");
const require = createRequire(join(FRONTEND, "package.json"));
const ts = require("typescript");

let dir;
let T; // TraceTimeline module
let L; // LatencyStrip module
let React;
let render;

before(async () => {
  dir = mkdtempSync(join(FRONTEND, ".kit-render-"));
  for (const name of readdirSync(CORE).filter((n) => /\.(ts|tsx)$/.test(n))) {
    const src = readFileSync(join(CORE, name), "utf8");
    const out = ts.transpileModule(src, {
      compilerOptions: {
        module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
        verbatimModuleSyntax: false, isolatedModules: true,
      },
      fileName: name,
    }).outputText.replace(/(from\s+|import\s*\(\s*|import\s+)(["'])(\.\/[^"']+)\2/g, "$1$2$3.mjs$2");
    writeFileSync(join(dir, name.replace(/\.(ts|tsx)$/, ".mjs")), out);
  }
  T = await import(pathToFileURL(join(dir, "TraceTimeline.mjs")).href);
  L = await import(pathToFileURL(join(dir, "LatencyStrip.mjs")).href);
  React = require("react");
  ({ renderToStaticMarkup: render } = require("react-dom/server"));
});

after(() => {
  if (dir) rmSync(dir, { recursive: true, force: true });
});

const h = (...a) => React.createElement(...a);
const text = (html) => html.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();

test("formatDuration and sinceStart", () => {
  const f = T.formatDuration;
  assert.equal(f(0), "0 ms");
  assert.equal(f(420.4), "420 ms");
  assert.equal(f(1234), "1.2 s");
  assert.equal(f(12_400), "12 s");
  assert.equal(f(65_000), "1 m 05 s");
  assert.equal(f(2 * 3600_000 + 3 * 60_000), "2 h 03 m");
  for (const bad of [null, undefined, -1, Number.NaN, Infinity]) assert.equal(f(bad), "—");
  assert.equal(T.sinceStart("2026-03-14T10:00:05Z", "2026-03-14T10:00:00Z"), 5000);
  assert.equal(T.sinceStart("2026-03-14T09:00:00Z", "2026-03-14T10:00:00Z"), 0);
  assert.equal(T.sinceStart("nope", "2026-03-14T10:00:00Z"), 0);
});

const EVENTS = [
  { id: "a", at: "2026-03-14T10:00:00Z", label: "LLM", title: "invented_stage", detail: "model-a", tone: "success",
    amount: "$0.0100", durationMs: 900 },
  { id: "b", at: "2026-03-14T10:01:05Z", label: "DataForSEO", title: "invented_endpoint", tone: "danger",
    durationMs: null },
];

test("TraceTimeline renders an ordered list with offsets, labels and figures", () => {
  const html = render(h(T.TraceTimeline, { events: EVENTS, testId: "tl", ariaLabel: "Calls of the run" }));
  assert.match(html, /^<ol[^>]*data-testid="tl"/);
  assert.match(html, /aria-label="Calls of the run"/);
  assert.equal((html.match(/data-kit-timeline-row/g) ?? []).length, 2);
  const t = text(html);
  assert.match(t, /\+0 ms LLM invented_stage model-a \$0\.0100 900 ms/);
  assert.match(t, /\+1 m 05 s DataForSEO invented_endpoint/);
  assert.ok(!/<button/.test(html), "no button without onSelect");
});

test("TraceTimeline rows are buttons with onSelect; empty text; scroll box; explicit start", () => {
  const html = render(h(T.TraceTimeline, { events: EVENTS, onSelect: () => {}, maxHeight: 300,
    start: "2026-03-14T09:59:58Z" }));
  assert.equal((html.match(/<button type="button"/g) ?? []).length, 2);
  assert.match(html, /max-height:300px/);
  assert.match(text(html), /^\+2\.0 s/);
  const empty = render(h(T.TraceTimeline, { events: [], testId: "e" }));
  assert.match(empty, /data-testid="e"/);
  assert.match(text(empty), /Nothing happened in this run/);
  assert.match(text(render(h(T.TraceTimeline, { events: [], emptyText: "No calls" }))), /^No calls$/);
});

test("latencyStats: nearest-rank percentiles of the timed values", () => {
  assert.deepEqual(L.latencyStats([]), { count: 0, p50: null, p95: null, max: null });
  assert.deepEqual(L.latencyStats([null, undefined, -5, Number.NaN]), { count: 0, p50: null, p95: null, max: null });
  const v = Array.from({ length: 20 }, (_, i) => (i + 1) * 100);
  assert.deepEqual(L.latencyStats([...v, null]), { count: 20, p50: 1000, p95: 1900, max: 2000 });
  assert.deepEqual(L.latencyStats([300]), { count: 1, p50: 300, p95: 300, max: 300 });
});

test("LatencyStrip draws one bar per call and labels the image with the summary", () => {
  const html = render(h(L.LatencyStrip, { points: [{ ms: 100 }, { ms: 400, tone: "danger" }, { ms: null }],
    testId: "ls", height: 20 }));
  assert.match(html, /data-testid="ls"/);
  assert.match(html, /role="img" aria-label="p50 100 ms · p95 400 ms · slowest 400 ms · 2 timed calls"/);
  assert.equal((html.match(/<span title=|<span style=/g) ?? []).length >= 3, true);
  assert.match(html, /height:20px[^"]*"[^>]*><\/span>/, "the slowest call fills the strip");
  const none = render(h(L.LatencyStrip, { points: [{ ms: null }] }));
  assert.match(none, /aria-label="No call has a time"/);
});

test("maxBars keeps the last calls but the figures count every call", () => {
  const points = Array.from({ length: 10 }, (_, i) => ({ ms: (i + 1) * 10 }));
  const html = render(h(L.LatencyStrip, { points, maxBars: 3 }));
  assert.match(html, /10 timed calls/);
  assert.equal((html.match(/title="/g) ?? []).length, 3);
});
