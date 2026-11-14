// frontend/lib/docsGroups.ts — AA-1104: numbered, collapsible groups of the docs reader list (/admin/docs).
//
// The list groups the catalog by folder (groupBySection). Each group gets a display number (1, 2, 3…) and each page
// in it a sub-number (1.1, 1.2…), counted over the whole catalog in the order the list already uses, so a search or
// a filter hides rows without renumbering the rest. Numbers are display only: the catalog id stays the page's id.
//
// Groups fold. Which ones are open: the viewer's last choice remembered in this browser (localStorage, best effort),
// or, before any choice, the group of the page being read (the first group when none). While a search query is
// typed, every group with a match opens on its own and shows only the matching pages; a group the viewer folds
// during that search stays folded until the query changes. Pure functions only: the page holds the state.

import { groupBySection, type DocEntry } from "./docsReader.ts";

export const DOC_GROUPS_STORAGE_KEY = "aa_docs_open_groups";

export type NumberedDoc = { doc: DocEntry; number: string };
export type NumberedGroup = { section: string; number: number; pages: NumberedDoc[] };

/** The catalog in list order, numbered: group n, page n.m. */
export function numberGroups(pages: DocEntry[]): NumberedGroup[] {
  return groupBySection(pages).map((g, gi) => ({
    section: g.section,
    number: gi + 1,
    pages: g.pages.map((doc, pi) => ({ doc, number: `${gi + 1}.${pi + 1}` })),
  }));
}

/** The numbered groups narrowed to the pages that pass `keep`, numbers unchanged; a group left empty is dropped. */
export function filterGroups(groups: NumberedGroup[], keep: (doc: DocEntry) => boolean): NumberedGroup[] {
  const out: NumberedGroup[] = [];
  for (const g of groups) {
    const pages = g.pages.filter((p) => keep(p.doc));
    if (pages.length) out.push({ ...g, pages });
  }
  return out;
}

/** The group (section) a page path is listed under, or null when no group holds it. */
export function sectionOfPath(groups: NumberedGroup[], path: string | null | undefined): string | null {
  if (!path) return null;
  return groups.find((g) => g.pages.some((p) => p.doc.path === path))?.section ?? null;
}

/** Before the viewer has chosen: the group of the page being read, else the first group, else none. */
export function defaultOpenGroups(groups: NumberedGroup[], selectedSection: string | null): string[] {
  if (selectedSection && groups.some((g) => g.section === selectedSection)) return [selectedSection];
  return groups.length ? [groups[0].section] : [];
}

/** The remembered open groups (a JSON array of section names); null when nothing usable is stored. */
export function parseOpenGroups(raw: string | null | undefined): string[] | null {
  if (raw == null) return null;
  try {
    const v: unknown = JSON.parse(raw);
    return Array.isArray(v) ? v.filter((s): s is string => typeof s === "string") : null;
  } catch {
    return null;
  }
}

export function serializeOpenGroups(sections: Iterable<string>): string {
  return JSON.stringify([...new Set(sections)].sort());
}

/** `open` with `section` opened (same array when it already is). */
export function withOpenGroup(open: string[], section: string): string[] {
  return open.includes(section) ? open : [...open, section];
}

/** `open` with `section` toggled. */
export function toggleGroup(open: string[], section: string): string[] {
  return open.includes(section) ? open.filter((s) => s !== section) : [...open, section];
}

export type OpenState = {
  /** The groups shown in the list (already narrowed by search and filters). */
  groups: NumberedGroup[];
  /** The viewer's open groups (remembered, or the default). */
  open: string[];
  /** The search text as typed. */
  query: string;
  /** Groups the viewer folded during the current search. */
  foldedInSearch: string[];
};

/** The sections shown open: during a search every group with a match minus the ones folded in it, else `open`. */
export function openSections({ groups, open, query, foldedInSearch }: OpenState): Set<string> {
  if (query.trim()) return new Set(groups.map((g) => g.section).filter((s) => !foldedInSearch.includes(s)));
  return new Set(open);
}
