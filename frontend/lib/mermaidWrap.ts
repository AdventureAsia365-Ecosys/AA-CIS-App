// lib/mermaidWrap.ts — the label wrapping width of the docs reader's Mermaid diagrams (app/_kit/MermaidDiagram.tsx),
// kept pure so it is unit-tested (lib/mermaidWrap.test.mjs).
//
// The renderer uses plain SVG labels (htmlLabels off, see MermaidDiagram), so CSS can not steer the wrapping: mermaid
// itself splits a label into lines no wider than `flowchart.wrappingWidth` (120px by default in mermaid 12) and, when
// a single word is wider than that, cuts the word per character ("AA-TripPlanne|r-Web", "aa-cis-dev-ap|i"). So the
// width is chosen per diagram: at least BASE_WRAP_WIDTH, widened to the widest single word of the source, so a word
// never breaks while multi-word labels still wrap at spaces. The node boxes are sized from the drawn text, so they
// follow.

/** Width multi-word labels wrap at (px): mermaid's earlier default, readable boxes without long single lines. */
export const BASE_WRAP_WIDTH = 200;

/** Upper bound (px), so one pathological token can not make every box of the diagram that wide. */
export const MAX_WRAP_WIDTH = 480;

/** Mermaid's label font size (themeVariables.fontSize default). */
export const LABEL_FONT_PX = 16;

// Slack for the difference between the measure used here (canvas) and mermaid's (SVG getComputedTextLength).
const SLACK_RATIO = 1.04;
const SLACK_PX = 6;

/**
 * The words of a diagram source as mermaid would wrap them: split at white space, line breaks (`<br>`, `\n`) and the
 * syntax that delimits labels (brackets, pipes, quotes). Over-counting (node ids, arrows) only widens the wrap.
 */
export function sourceWords(source: string): string[] {
  return source
    .replace(/<br\s*\/?>/gi, " ")
    .replace(/\\n/g, " ")
    .split(/[\s[\](){}|"<>`]+/)
    .filter(Boolean);
}

/**
 * The flowchart wrapping width for a source: the widest word (as `measure` reports it, in px) plus slack, never
 * below BASE_WRAP_WIDTH nor above MAX_WRAP_WIDTH.
 */
export function wrappingWidthFor(source: string, measure: (word: string) => number): number {
  let widest = 0;
  for (const word of sourceWords(source)) widest = Math.max(widest, measure(word));
  const needed = Math.ceil(widest * SLACK_RATIO + SLACK_PX);
  return Math.min(MAX_WRAP_WIDTH, Math.max(BASE_WRAP_WIDTH, needed));
}

/** Rough width when no canvas is available: a wide-ish average glyph for a geometric sans at 16px. */
export function estimateWidth(word: string, fontPx: number = LABEL_FONT_PX): number {
  return word.length * fontPx * 0.62;
}
