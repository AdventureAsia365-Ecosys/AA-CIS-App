// AA-898 — GitHub alerts as callouts (lib/markdownCallouts.ts), on the real remark parser the reader uses.
import assert from "node:assert/strict";
import { test } from "node:test";
import { unified } from "unified";
import remarkParse from "remark-parse";
import remarkGfm from "remark-gfm";
import remarkRehype from "remark-rehype";

import { calloutKindOf, remarkCallouts } from "./markdownCallouts.ts";

async function hast(md) {
  const proc = unified().use(remarkParse).use(remarkGfm).use(remarkCallouts).use(remarkRehype);
  return proc.run(proc.parse(md));
}
const quotes = (tree) => tree.children.filter((n) => n.type === "element" && n.tagName === "blockquote");
const text = (n) => (n.type === "text" ? n.value : (n.children ?? []).map(text).join(""));

test("each GitHub alert kind becomes a callout and loses its marker", async () => {
  for (const kind of ["NOTE", "TIP", "IMPORTANT", "WARNING", "CAUTION"]) {
    const [q] = quotes(await hast(`> [!${kind}]\n> Read **this** first.\n`));
    assert.equal(calloutKindOf(q), kind.toLowerCase());
    assert.equal(text(q).trim(), "Read this first.");
    assert.ok(!text(q).includes("[!"));
  }
});

test("marker alone in the quote, and with more paragraphs", async () => {
  const [q] = quotes(await hast("> [!WARNING]\n>\n> First.\n>\n> Second.\n"));
  assert.equal(calloutKindOf(q), "warning");
  assert.deepEqual(q.children.filter((c) => c.tagName === "p").map(text), ["First.", "Second."]);
});

test("ordinary quotes and look-alikes stay plain", async () => {
  for (const md of ["> Just a quote.\n", "> [!note] lower case\n", "> Text [!NOTE] later\n", "> [!DANGER]\n> x\n",
    "> **[!NOTE]** bold\n"]) {
    const [q] = quotes(await hast(md));
    assert.equal(calloutKindOf(q), null, md);
  }
});

test("markup inside a callout stays Markdown, raw HTML is not introduced", async () => {
  const [q] = quotes(await hast("> [!NOTE]\n> Use <img src=x onerror=alert(1)> and [a link](https://example.com)\n>\n> <script>alert(1)</script>\n"));
  assert.equal(calloutKindOf(q), "note");
  const kinds = JSON.stringify(q);
  assert.ok(kinds.includes('"tagName":"a"'));
  assert.ok(!kinds.includes('"tagName":"script"') && !kinds.includes('"tagName":"img"'));
  assert.ok(!kinds.includes('"type":"raw"') && !kinds.includes("onerror"));   // raw HTML never reaches hast
});
