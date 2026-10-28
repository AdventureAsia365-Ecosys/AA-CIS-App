// AA-898 — the docs reader's Mermaid output check (lib/mermaidSafety.ts).
import assert from "node:assert/strict";
import { test } from "node:test";

import { MERMAID_SECURE_KEYS, svgIsSelfContained } from "./mermaidSafety.ts";

test("mermaid's own output (fragment markers, xmlns) is self-contained", () => {
  const svg = '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" id="aa-mermaid-1">'
    + '<style>#aa-mermaid-1 .node rect{fill:#fff;}</style><path marker-end="url(#aa-mermaid-1_flowchart-pointEnd)"/>'
    + "<a><text>url and src= in a label, href=x</text></a></svg>";
  assert.equal(svgIsSelfContained(svg), true);
});

test("anything that would fetch is refused", () => {
  for (const bad of [
    '<style>.n{background:url(https://evil.example/p.png)}</style>',
    "<style>.n{background:url( 'https://evil.example/p.png')}</style>",
    '<style>@import "https://evil.example/x.css";</style>',
    '<rect style="fill:url(//evil.example/x)"/>',
    '<a href="https://evil.example/">x</a>',
    "<a xlink:href='javascript:alert(1)'>x</a>",
    '<feImage src="https://evil.example/x"/>',
  ]) assert.equal(svgIsSelfContained(`<svg>${bad}</svg>`), false, bad);
});

test("a page directive can not change the security, label, theme or CSS keys", () => {
  for (const k of ["securityLevel", "htmlLabels", "theme", "themeCSS", "themeVariables", "fontFamily", "secure"]) {
    assert.ok(MERMAID_SECURE_KEYS.includes(k), k);
  }
});
