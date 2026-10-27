// app/_kit/guideContent.ts — AA-919: the checks of an admin page's Guide tab (CIS-local, root ADR 0016 decision 1).
//
// Every admin page with a Guide tab uses this pattern, with no per-page variant:
//   1. the content is a `GuideContent` in `app/admin/<page>/guide.ts` (data only: plain strings, no JSX);
//   2. the page adds `{ key: "guide", label: "Guide" }` to its kit `Tabs` (after the daily-use tabs) and renders
//      `<GuidePanel content={…} />` (app/_kit/core/GuidePanel.tsx) for it, plus a short "New here? Read the Guide"
//      link in its PageHeader description that switches to that tab;
//   3. the guide is registered in `lib/guides.test.mjs`, which runs `guideProblems` (shape) and checks that every
//      `uiLabels` term still appears in the page's code, so a renamed label fails CI.
// Guides are English (admin UI default) and quote the page's own labels verbatim. Public repo: no ids, ARNs or
// tenant names in a guide.
//
// The content shape and the render helpers (types, captions, `guideAnchor`, `guideOutline`) are part of the kit core
// (app/_kit/core/guide.ts, AA-994 S1a) and re-exported here, so importers of this module do not change.
// Pure functions only: `node --test --experimental-strip-types` loads this file and core/guide.ts directly, which is
// why the import below names the `.ts` file (tsconfig `allowImportingTsExtensions`).

import { GUIDE_GLOSSARY_ID, type GuideContent, type GuideTerm } from "./core/guide.ts";

export * from "./core/guide.ts";

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
