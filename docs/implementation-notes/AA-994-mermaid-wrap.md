# AA-994 — docs reader: Mermaid labels never break mid-word (S256)

Follow-up to the S256 UI evidence of the docs reader (`/admin/docs`): the ecosystem diagram still showed
"AA-TripPlanne|r-Web", "aa-cis-dev-ap|i", "aa-booking-d|ev-api", "AA3-Bedrock-I|nvoker" after CIS #1115 (wait for
Poppins before drawing). The font was not the cause.

## Decisions
- **Cause**: with SVG labels (`htmlLabels: false`) mermaid 12 wraps each label to `flowchart.wrappingWidth`
  (default **120px** in 12.x) and, when one word is wider than that, cuts the word per character
  (`splitWordToFitWidth`). Long hyphenated names at 16px Poppins are 150–220px wide.
- **Fix**: the wrapping width is chosen per diagram (`lib/mermaidWrap.ts`): the widest word of the source, measured
  on a canvas in the label font (the wider of 400 and 700 weight), plus 4% + 6px slack, never below 200px (multi-word
  labels still wrap at spaces) nor above 480px (one absurd token can not make every box huge). `MermaidDiagram`
  passes it as `flowchart.wrappingWidth` on every render.
- **Not HTML labels + CSS**: HTML labels need `foreignObject`, which the renderer forbids on purpose (AA-898
  security: DOMPurify strips it). With SVG text the wrapping is computed by mermaid in JS, so CSS
  (`white-space`, `word-break`) has no effect; the width is the only lever.

## Changed
- `frontend/lib/mermaidWrap.ts` (new, pure) + `frontend/lib/mermaidWrap.test.mjs` (runs with `npm run test:unit`).
- `frontend/app/_kit/MermaidDiagram.tsx`: canvas measure + `wrappingWidth` in `mermaid.initialize`.

## Tradeoffs
- Word extraction splits the raw source at white space and label delimiters, so node ids and arrows are counted too;
  that can only widen the wrap a little, never cut a word. Parsing mermaid syntax per diagram type was not worth it.
- A diagram with one long word gets slightly wider boxes everywhere (e.g. 259px for the ecosystem diagram).
- Canvas `measureText` and SVG `getComputedTextLength` differ by a pixel or two; the slack covers it. No canvas →
  a length-based estimate (0.62em per character).

## Should know
- `wrappingWidth` is not in `MERMAID_SECURE_KEYS`, so a page's `%%{init}%%` may still set it (layout only, no
  security impact).
- Visual check: the ecosystem-architecture block rendered with mermaid 12.2.0 + this config in Playwright, before
  and after (root `docs/audits/2026-10-29-S256-ui-evidence/03f-mermaid-wrap-{before,after}.png`): 12 split words
  before, 0 after, no label wider than its box.
