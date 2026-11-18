// lib/docsSearchView.ts — AA-1133 follow-up (S284 live check): how the docs reader shows full-text search hits
// (app/admin/docs/DocSearchResults.tsx). No React here, so node --test can load it (lib/docsSearchView.test.mjs).
//
// - Reading language first: a page and its translation (<page>.vi.md) are one pair; the pair keeps the place of its
//   better-ranked copy, and inside the pair the copy in the reader's language comes first. Nothing is dropped.
// - A section row without snippet text (a heading straight followed by a sub-heading: the index has no body words for
//   it) is left out: a bare heading says nothing about why the page matched.

/** The original page a hit belongs to: `x.vi.md` -> `x.md` (the translation naming rule, root docs/STYLE.md). */
export function pairKey(path: string): string {
  return path.replace(/\.[a-z]{2}\.md$/i, ".md");
}

/** The hits in the server's order, except that inside each page / translation pair the copy in `lang` comes first. */
export function rankByLanguage<T extends { path: string; lang: string }>(results: readonly T[], lang: string): T[] {
  const pairs = new Map<string, T[]>();
  for (const r of results) {
    const key = pairKey(r.path);
    const list = pairs.get(key);
    if (list) list.push(r);
    else pairs.set(key, [r]);
  }
  const out: T[] = [];
  for (const list of pairs.values()) {
    out.push(...list.filter((r) => r.lang === lang), ...list.filter((r) => r.lang !== lang));
  }
  return out;
}

type Segment = { text: string };

/** The sections worth a row: those whose snippet has words in it. */
export function sectionsWithText<S extends { snippet: readonly Segment[] }>(sections: readonly S[]): S[] {
  return sections.filter((s) => s.snippet.some((seg) => seg.text.trim() !== ""));
}
