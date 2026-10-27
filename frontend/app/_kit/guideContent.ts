// app/_kit/guideContent.ts — AA-919: the one data shape of an admin page's Guide tab, and its checks.
//
// Every admin page with a Guide tab uses this pattern, with no per-page variant:
//   1. the content is a `GuideContent` in `app/admin/<page>/guide.ts` (data only: plain strings, no JSX);
//   2. the page adds `{ key: "guide", label: "Guide" }` to its kit `Tabs` (after the daily-use tabs) and renders
//      `<GuidePanel content={…} />` (app/_kit/GuidePanel.tsx) for it, plus a short "New here? Read the Guide" link
//      in its PageHeader description that switches to that tab;
//   3. the guide is registered in `lib/guides.test.mjs`, which runs `guideProblems` (shape) and checks that every
//      `uiLabels` term still appears in the page's code, so a renamed label fails CI.
// Guides are English (admin UI default) and quote the page's own labels verbatim. Public repo: no ids, ARNs or
// tenant names in a guide.
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
   * shows it as a chip, and the guide test checks it still exists in the page's code. Several labels of one term are
   * joined with " / " ("L0 passed / L0 failed"). */
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

const ID_RE = /^[a-z0-9]+(-[a-z0-9]+)*$/;

function blank(s: unknown): boolean {
  return typeof s !== "string" || s.trim() === "";
}

function termProblems(items: GuideTerm[], where: string): string[] {
  const out: string[] = [];
  if (!items.length) out.push(`${where}: no terms`);
  const seen = new Set<string>();
  for (const t of items) {
    if (blank(t.term)) out.push(`${where}: a term is empty`);
    if (blank(t.text)) out.push(`${where}: "${t.term}" has no text`);
    const key = (t.term || "").trim().toLowerCase();
    if (seen.has(key)) out.push(`${where}: "${t.term}" twice`);
    seen.add(key);
  }
  return out;
}

/** Everything wrong with a guide's shape (empty = fine): a title, at least one section, every section with a valid
 * unique id and a heading and at least one block, no empty string anywhere, no empty list. One check for every guide
 * (lib/guides.test.mjs). */
export function guideProblems(g: GuideContent): string[] {
  const out: string[] = [];
  if (blank(g.title)) out.push("the guide has no title");
  if (g.intro !== undefined && blank(g.intro)) out.push("the intro is empty");
  if (!g.sections.length) out.push("the guide has no section");
  const ids = new Set<string>();
  for (const s of g.sections) {
    const where = `section ${s.id || "(no id)"}`;
    if (blank(s.id)) out.push("a section has no id");
    else if (!ID_RE.test(s.id)) out.push(`${where}: the id is not lower-case-with-dashes`);
    if (s.id === GUIDE_GLOSSARY_ID && g.glossary) out.push(`${where}: the id is kept for the glossary`);
    if (ids.has(s.id)) out.push(`${where}: the id is used twice`);
    ids.add(s.id);
    if (blank(s.heading)) out.push(`${where}: no heading`);
    if (!s.blocks.length) out.push(`${where}: no block`);
    s.blocks.forEach((b, i) => {
      const at = `${where}, block ${i + 1} (${b.kind})`;
      switch (b.kind) {
        case "paragraph":
        case "subheading":
          if (blank(b.text)) out.push(`${at}: empty text`);
          break;
        case "list":
          if (!b.items.length) out.push(`${at}: no items`);
          b.items.forEach((x, j) => { if (blank(x)) out.push(`${at}: item ${j + 1} is empty`); });
          break;
        case "callout":
          if (blank(b.text)) out.push(`${at}: empty text`);
          if (b.title !== undefined && blank(b.title)) out.push(`${at}: empty title`);
          if (b.tone !== "note" && b.tone !== "warning") out.push(`${at}: unknown tone`);
          break;
        case "terms":
          out.push(...termProblems(b.items, at));
          break;
        case "flow":
          if (b.steps.length < 2) out.push(`${at}: a flow needs at least 2 steps`);
          b.steps.forEach((x, j) => { if (blank(x)) out.push(`${at}: step ${j + 1} is empty`); });
          break;
        case "control":
          if (blank(b.label)) out.push(`${at}: no label`);
          if (blank(b.does)) out.push(`${at}: "${b.label}" says nothing about what it does`);
          for (const k of ["cost", "when", "avoid"] as const) {
            if (b[k] !== undefined && blank(b[k])) out.push(`${at}: "${b.label}" has an empty ${k}`);
          }
          break;
        default:
          out.push(`${at}: unknown block kind`);
      }
    });
  }
  if (g.glossary) out.push(...termProblems(g.glossary, "glossary"));
  return out;
}

/** Every page label a guide documents: the terms of its `uiLabels` blocks, each " / " part on its own. */
export function guideUiLabels(g: GuideContent): string[] {
  const out: string[] = [];
  for (const s of g.sections) {
    for (const b of s.blocks) {
      if (b.kind === "terms" && b.uiLabels) for (const t of b.items) out.push(...t.term.split(" / ").map((x) => x.trim()));
    }
  }
  return out;
}

/** The labels of every `control` block (the page's buttons, as the guide names them). */
export function guideControlLabels(g: GuideContent): string[] {
  return g.sections.flatMap((s) => s.blocks.flatMap((b) => (b.kind === "control" ? [b.label] : [])));
}
