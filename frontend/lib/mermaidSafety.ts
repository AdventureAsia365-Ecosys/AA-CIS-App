// lib/mermaidSafety.ts — AA-898: the safety rules of the docs reader's Mermaid renderer (app/_kit/MermaidDiagram.tsx),
// kept pure so they are unit-tested (lib/mermaidSafety.test.mjs).

// Config keys a diagram's own %%{init}%% directive may not override (mermaid's defaults plus labels, theme and CSS).
export const MERMAID_SECURE_KEYS = [
  "secure", "securityLevel", "startOnLoad", "maxTextSize", "suppressErrorRendering", "maxEdges",
  "htmlLabels", "theme", "themeCSS", "themeVariables", "darkMode", "fontFamily", "altFontFamily",
];

// Anything in the sanitised SVG that would make the browser fetch something: url(...) not to a #fragment, @import,
// or an href / src attribute that is not a #fragment. (Label text is entity-encoded, so a quote after "href=" can
// only be an attribute; a label that merely says "url(" falls back to the source, which is safe.)
const EXTERNAL_REF = /url\(\s*(?!['"]?#)|@import|\s(?:xlink:)?href\s*=\s*["'](?!#)|\ssrc\s*=\s*["']/i;

/** True when the SVG only refers to itself (no remote fetch of any kind). */
export function svgIsSelfContained(svg: string): boolean {
  return !EXTERNAL_REF.test(svg);
}
