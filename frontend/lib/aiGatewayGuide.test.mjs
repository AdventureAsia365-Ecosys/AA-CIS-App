// frontend/lib/aiGatewayGuide.test.mjs
// AA-1042 (S270): the Guide tab of AI gateway (backend GET /admin/ai-gateway/guide). Pure helpers: which language of
// the page shows, how a link in the page renders (the docs reader for a reader, plain text for everyone else, never a
// broken in-app link), the rows of the live tables (the serving deployment, not the chain's first key), and the page
// text without issue references. Text guards: the tab is wired into the page, kit only, its test ids, and its
// messages have the same keys in English and Vietnamese with no issue ID. Invented names only.
// Run: npm run test:unit (node --test with --experimental-strip-types).

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import {
  GUIDE_ASSET_API, cellTone, coverageRows, docView, guideImage, guideLink, matrixRows, routeRows,
  stripIssueIdsMarkdown,
} from "./aiGatewayGuide.ts";

const read = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), "utf8");

const PATH = "docs/architecture/ai-gateway.md";
const DOC = {
  missing: false, id: "ARCH-01-ai-gateway", repo: "AA-Ecosys", path: PATH, title: "Zz gateway", lang: "en",
  markdown: "# Zz\n", source: { repo: "AA-Ecosys", sha: "b".repeat(40) }, generated_at: null,
  translations: [{ lang: "vi", path: "docs/architecture/ai-gateway.vi.md", title: "Cổng zz", markdown: "# Cổng\n" }],
  links: { reader: true, published: [PATH, "docs/architecture/ai-gateway.vi.md", "docs/generated/llm-routes.md",
    "docs/architecture/ai-use-register.md", "docs/adr/0009-zz-gateway.md"] },
};

test("docView follows the app language and falls back to English with a note", () => {
  const en = docView({ doc: DOC, doc_reason: null }, "en");
  assert.equal(en.kind, "page");
  assert.deepEqual([en.lang, en.path, en.fallback, en.available], ["en", PATH, false, ["en", "vi"]]);
  const vi = docView({ doc: DOC, doc_reason: null }, "vi");
  assert.deepEqual([vi.lang, vi.path, vi.markdown, vi.title], ["vi", "docs/architecture/ai-gateway.vi.md",
    "# Cổng\n", "Cổng zz"]);
  const noVi = docView({ doc: { ...DOC, translations: [] }, doc_reason: null }, "vi");
  assert.deepEqual([noVi.lang, noVi.fallback], ["en", true]);
});

test("docView: a missing page and an owner page are notes, not errors", () => {
  assert.deepEqual(docView({ doc: { missing: true, reason: "not_published" }, doc_reason: null }, "en"),
    { kind: "missing", reason: "not_published" });
  assert.deepEqual(docView({ doc: null, doc_reason: "owner_tier" }, "en"), { kind: "restricted", reason: "owner_tier" });
  assert.equal(docView(undefined, "en").kind, "restricted");
});

test("guideLink opens the docs reader for a reader, with the page's relative links resolved", () => {
  assert.deepEqual(guideLink("../adr/0009-zz-gateway.md", PATH, DOC.links, true),
    { kind: "internal", href: "/admin/docs?path=docs%2Fadr%2F0009-zz-gateway.md", path: "docs/adr/0009-zz-gateway.md",
      hash: "" });
  assert.equal(guideLink("../generated/llm-routes.md", PATH, DOC.links, true).href,
    "/admin/docs?path=docs%2Fgenerated%2Fllm-routes.md");
  assert.equal(guideLink("ai-use-register.md#stages", PATH, DOC.links, true).href,
    "/admin/docs?path=docs%2Farchitecture%2Fai-use-register.md#doc-stages");
  // from the Vietnamese copy the same relative link lands on the same page
  assert.equal(guideLink("ai-use-register.md", "docs/architecture/ai-gateway.vi.md", DOC.links, true).kind,
    "internal");
});

test("guideLink: plain text for anyone who may not open the reader, and for unpublished targets", () => {
  // the backend says reader, but the viewer's role has no /admin/docs page (and the other way round)
  assert.deepEqual(guideLink("../adr/0009-zz-gateway.md", PATH, DOC.links, false), { kind: "text" });
  assert.deepEqual(guideLink("../adr/0009-zz-gateway.md", PATH, { reader: false, published: [] }, true),
    { kind: "text" });
  assert.deepEqual(guideLink("../security/zz-register.md", PATH, DOC.links, true), { kind: "text" });
  assert.deepEqual(guideLink("diagrams/README.md", PATH, undefined, true), { kind: "text" });
  assert.deepEqual(guideLink("#4-routing", PATH, DOC.links, false), { kind: "anchor", id: "4-routing" });
  assert.deepEqual(guideLink("https://example.com/zz", PATH, DOC.links, false),
    { kind: "external", href: "https://example.com/zz" });
  assert.deepEqual(guideLink("javascript:alert(1)", PATH, DOC.links, true), { kind: "text" });
});

const ROUTES = {
  version: 6, order_mode: "model_first", stages: [
    { stage: "q7_write", role: "writer", is_active: true, chain: ["zeta-5-5", "zeta-4-1"], first: "zeta-5-5@zeta-direct",
      first_model: "zeta-5-5", first_connection: "zeta-direct", skipped: [] },
    { stage: "q7_check", role: "validate", is_active: true, chain: ["zeta-5-5", "zeta-4-1"], first: "zeta-4-1@acc8",
      first_model: "zeta-4-1", first_connection: "acc8",
      skipped: [{ model: "zeta-5-5", connection: "acc8", reason: "refused (access_denied)" }] },
    { stage: "q7_dead", role: "writer", is_active: false, chain: ["zeta-5-5"], first: null, first_model: null,
      first_connection: null, skipped: [] },
    { stage: "q7_none", role: "judge", is_active: true, chain: ["zeta-9"], first: null, first_model: null,
      first_connection: null, skipped: [] },
  ], provider_order: [],
};

test("routeRows: a refused first deployment is never shown as serving; the fallback is flagged", () => {
  const rows = routeRows(ROUTES);
  assert.deepEqual(rows.map((r) => r.stage), ["q7_check", "q7_none", "q7_write", "q7_dead"]);
  const check = rows[0];
  assert.equal(check.first, "zeta-4-1@acc8");
  assert.equal(check.servedByFallback, true);
  assert.deepEqual(check.skipped, ["zeta-5-5@acc8: refused (access_denied)"]);
  assert.equal(rows.find((r) => r.stage === "q7_write").servedByFallback, false);
  assert.equal(rows.find((r) => r.stage === "q7_dead").first, null);
  assert.equal(routeRows(null).length, 0);
});

test("matrixRows orders each model's connections as the gateway tries them; cellTone reads the probe", () => {
  const matrix = {
    models: [{ model_key: "zeta-5-5", label: "Zeta 5.5", family: "zeta", enabled: true }],
    connections: [{ connection_key: "acc8" }, { connection_key: "zeta-direct" }],
    cells: [
      { model_key: "zeta-5-5", connection_key: "acc8", enabled: true, priority: 10, access_state: "access_denied",
        last_probe_at: "2026-11-05T05:00:00Z" },
      { model_key: "zeta-5-5", connection_key: "zeta-direct", enabled: true, priority: 5, access_state: "ok",
        last_probe_at: null },
    ],
  };
  const { connections, rows } = matrixRows(matrix);
  assert.deepEqual(connections, ["acc8", "zeta-direct"]);
  assert.deepEqual(rows[0].order, ["zeta-direct", "acc8"]);
  assert.equal(cellTone(rows[0].cells["acc8"]), "refused");
  assert.equal(cellTone(rows[0].cells["zeta-direct"]), "ok");
  assert.equal(cellTone(undefined), "none");
  assert.equal(cellTone({ ...matrix.cells[1], enabled: false }), "off");
  assert.equal(cellTone({ ...matrix.cells[1], access_state: "unknown" }), "unknown");
  assert.deepEqual(matrixRows(null), { connections: [], rows: [] });
});

test("coverageRows: no-live stages first, then live, then the switched-off list", () => {
  const rows = coverageRows({ ready: true, uncovered: ["q7_none"], disabled: ["q7_dead"], stages: [
    { stage: "q7_write", role: "writer", status: "live", live_model: "zeta-5-5", live_connection: "zeta-direct",
      unproven: 0 },
    { stage: "q7_none", role: "judge", status: "none", live_model: null, live_connection: null, unproven: 1 }] });
  assert.deepEqual(rows.map((r) => [r.stage, r.status, r.live]),
    [["q7_none", "none", null], ["q7_write", "live", "zeta-5-5@zeta-direct"], ["q7_dead", "disabled", null]]);
  assert.deepEqual(coverageRows({ ready: false, stages: [], uncovered: [], disabled: [] }), []);
});

test("stripIssueIdsMarkdown keeps the Markdown structure, fences and code spans", () => {
  const md = [
    "- **Route trace** (AA-1042): every row.",
    "  The page (`/admin/zz`, AA-1042, CIS 370) changes routing.",
    "| Item | **Live** (AA-1042, S268) | AA-1002 S4, AA-1018, AA-1024 |",
    "See `AA-1002.md` for the notes.",
    "```text",
    "AA-1042 stays in a fence",
    "```",
    "AA-Partner and AA-CIS-App are repo names.",
  ].join("\n");
  assert.equal(stripIssueIdsMarkdown(md), [
    "- **Route trace**: every row.",
    "  The page (`/admin/zz`, CIS 370) changes routing.",
    "| Item | **Live** (S268) | |",
    "See `AA-1002.md` for the notes.",
    "```text",
    "AA-1042 stays in a fence",
    "```",
    "AA-Partner and AA-CIS-App are repo names.",
  ].join("\n"));
});

test("guideImage: an image the backend listed for the page loads through the tab's own route (AA-1064)", () => {
  const assets = ["docs/architecture/images/zz-panel.png", "docs/architecture/diagrams/zz-flow.svg",
    "docs/architecture/images/zz-panel-vi.webp"];
  assert.equal(GUIDE_ASSET_API, "/api/admin/ai-gateway/guide/asset");
  assert.equal(guideImage("images/zz-panel.png", PATH, assets),
    "/api/admin/ai-gateway/guide/asset?path=docs%2Farchitecture%2Fimages%2Fzz-panel.png");
  assert.equal(guideImage("./diagrams/zz-flow.svg?v=1", PATH, assets),
    "/api/admin/ai-gateway/guide/asset?path=docs%2Farchitecture%2Fdiagrams%2Fzz-flow.svg");
  assert.equal(guideImage("images/zz-panel-vi.webp", "docs/architecture/ai-gateway.vi.md", assets),
    "/api/admin/ai-gateway/guide/asset?path=docs%2Farchitecture%2Fimages%2Fzz-panel-vi.webp");
});

test("guideImage: anything not listed stays alt text, an older API answer (no assets) too", () => {
  const assets = ["docs/architecture/images/zz-panel.png", "docs/architecture/zz.md"];
  for (const src of [undefined, "", "#x", "images/other.png", "https://zz.example/zz-panel.png",
    "//zz.example/a.png", "data:image/png;base64,AAAA", "../../../../zz-panel.png", "zz.md"]) {
    assert.equal(guideImage(src, PATH, assets), null, String(src));
  }
  assert.equal(guideImage("images/zz-panel.png", PATH, undefined), null);
});

test("the Guide tab is wired into the page, kit only, with its test ids", () => {
  const page = read("../app/admin/ai-gateway/AiGateway.tsx");
  assert.match(page, /import GuideTab from "\.\/GuideTab"/);
  assert.match(page, /tab === "guide" && \(\s*<GuideTab howToRead=\{<GuidePanel content=\{guide\}/);
  const tab = read("../app/admin/ai-gateway/GuideTab.tsx");
  for (const id of ["ai-gateway-guide", "ai-gateway-guide-doc", "ai-gateway-guide-live-ready"]) {
    assert.ok(tab.includes(`"${id}"`), `test id ${id}`);
  }
  assert.match(tab, /<MarkdownView /, "the kit MarkdownView renders the page");
  assert.match(tab, /resolveImage=\{resolveImage\}/, "the page's images load (AA-1064)");
  assert.match(tab, /guideImage\(src, fromPath, doc\?\.assets\)/);
  assert.match(tab, /<DataTable<RouteRow>/);
  assert.doesNotMatch(tab, /from "react-markdown"|<table[\s>]/, "kit components only");
  assert.doesNotMatch(tab, /secret_value|api_key/i, "names only");
});

test("guide tab messages: same keys in English and Vietnamese, no issue ID", () => {
  const src = read("../app/admin/ai-gateway/guideTab.messages.ts");
  assert.doesNotMatch(src.replace(/^\/\/.*$/gm, ""), /AA-\d+/);
  const at = src.indexOf("\n  {\n", src.indexOf("export const GUIDE_TAB = defineMessages("));
  const split = src.indexOf("\n  {\n", at + 1);
  const end = src.indexOf("export const GUIDE_SOURCES");
  const keys = (part) => [...part.matchAll(/^ {4}([A-Za-z_]+):/gm)].map((m) => m[1]).sort();
  const en = keys(src.slice(at, split));
  assert.ok(en.length > 40);
  assert.deepEqual(keys(src.slice(split, end)), en);
  const sources = src.slice(end);
  const lines = sources.split("\n").filter((l) => l.trim().startsWith("{ routes:"));
  assert.equal(lines.length, 2);
  const k = (l) => [...l.matchAll(/([a-z]+):/g)].map((m) => m[1]);
  assert.deepEqual(k(lines[1]), k(lines[0]));
});
