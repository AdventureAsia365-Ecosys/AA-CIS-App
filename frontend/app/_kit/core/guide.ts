// UI kit core (root ADR 0016). Canonical: AA-CIS-App frontend/app/_kit/core. Edit only there; copies are checked byte for byte (kit.lock.json).
// app/_kit/core/guide.ts — the content shape GuidePanel renders (AA-919; split out of app/_kit/guideContent.ts in
// AA-994 S1a so GuidePanel is part of the portable kit core and takes its content as a prop).
//
// The page owns its content: it keeps a `GuideContent` as data (plain strings, no JSX) and passes it to
// `<GuidePanel content={…} />`. How an app checks its guides (CIS: app/_kit/guideContent.ts `guideProblems` and
// lib/guides.test.mjs) stays in that app.
//
// Data and pure functions only, no imports: `node --test --experimental-strip-types` loads this file directly.

/** The two callout tones: `note` (useful to know) and `warning` (a trap, a cost, or the code and docs disagreeing). */
export type GuideCalloutTone = "note" | "warning";

/** One term and what it means (a label on the page, or a glossary word). */
export interface GuideTerm { term: string; text: string }

/** A building block of a guide section, rendered in order by GuidePanel. */
export type GuideBlock =
  | { kind: "paragraph"; text: string }
  /** A sub-heading inside a section. */
  | { kind: "subheading"; text: string }
  | { kind: "list"; items: string[]; ordered?: boolean }
  | { kind: "callout"; tone: GuideCalloutTone; title?: string; text: string }
  /** Terms and meanings. `uiLabels`: each term is a label the page itself prints (a status, a badge); GuidePanel
   * shows it as a chip, and the app's guide test can check it still exists in the page's code. Several labels of one
   * term are joined with " / " ("L0 passed / L0 failed"). */
  | { kind: "terms"; items: GuideTerm[]; uiLabels?: boolean }
  /** A small diagram: steps joined by arrows (they wrap on a phone). */
  | { kind: "flow"; steps: string[] }
  /** One button or control of the page: its exact label, what it does, and optionally its cost, when to use it and
   * when not to. The captions are the kit's (GUIDE_CONTROL_CAPTIONS), the same on every page. */
  | { kind: "control"; label: string; does: string; cost?: string; when?: string; avoid?: string };

export interface GuideSection {
  /** Anchor of the section, unique in the guide: lower-case letters, digits and dashes. */
  id: string;
  /** Plain heading; GuidePanel numbers sections itself, so cross-references name the section, not its number. */
  heading: string;
  blocks: GuideBlock[];
}

export interface GuideContent {
  title: string;
  /** One or two sentences under the title: who the guide is for and how to read it. */
  intro?: string;
  sections: GuideSection[];
  /** Optional glossary, rendered as the last section (id `glossary`). */
  glossary?: GuideTerm[];
}

/** The captions of a `control` block, one wording for every guide. */
export const GUIDE_CONTROL_CAPTIONS = {
  does: "What it does",
  cost: "Cost and time",
  when: "Use it when",
  avoid: "Do not use it when",
} as const;

export const GUIDE_GLOSSARY_ID = "glossary";
export const GUIDE_GLOSSARY_HEADING = "Glossary";
/** A guide with at least this many sections (glossary included) gets an "On this page" table of contents. */
export const GUIDE_TOC_MIN_SECTIONS = 4;

/** The DOM id of a section's anchor (prefixed, so it never clashes with the page's own ids). */
export function guideAnchor(sectionId: string): string {
  return `guide-${sectionId}`;
}

/** The sections as GuidePanel shows them: content sections, then the glossary when there is one, numbered from 1. */
export function guideOutline(g: GuideContent): { id: string; heading: string; n: number }[] {
  const out = g.sections.map((s, i) => ({ id: s.id, heading: s.heading, n: i + 1 }));
  if (g.glossary && g.glossary.length) {
    out.push({ id: GUIDE_GLOSSARY_ID, heading: GUIDE_GLOSSARY_HEADING, n: out.length + 1 });
  }
  return out;
}
