"use client";
// app/admin/docs/DocsList.tsx — AA-1104: the docs reader's list, numbered and in collapsible groups.
//
// Groups are the sidebar groups of the bundle (AA-1133: front-matter `nav`, labels from DOC_NAV_MESSAGES), or the
// catalog folders (groupBySection) for an older bundle. AA-1133: while a full-text search runs, the list shows its
// hits (`searchPanel`) instead of the groups; without a search index the typed text filters titles, IDs and paths. Each group has a number (1, 2…) and each page a sub-number (1.1,
// 1.2…), counted over the whole catalog so search and filters never renumber (lib/docsGroups.ts); the catalog id
// stays on each row. A group header is a disclosure button (aria-expanded, aria-controls) with the page count.
// Open groups: the viewer's last choice in this browser (localStorage "aa_docs_open_groups", best effort; it can
// throw or be empty in a private window, then the choice lasts for this visit); before any choice, the group of the
// page being read, or the first group. Opening a page (a link, a shared URL) opens its group. While a search query is
// typed, every group with a match opens and shows only the matching pages; folding one during the search lasts until
// the query changes and is not remembered.
// AA-1132: when the manifest lists the generated "What changed in docs" page (root build_docs_reader.py rule 14), a
// link to it sits above the groups (it also stays in its own group, Reference).

import { useCallback, useEffect, useId, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { ChevronRight, History, Lock } from "lucide-react";
import { Button, EmptyState, K, Pressable, mono, sans, useT } from "../../_kit";
import { M } from "./messages";
import { DOC_NAV_MESSAGES, DOC_TYPE_MESSAGES } from "@/lib/docsReader.messages";
import { DOC_NAV_GROUPS, docsChangesPage, matchesFilter, type DocEntry, type DocNav } from "@/lib/docsReader";
import {
  DOC_GROUPS_STORAGE_KEY,
  defaultOpenGroups,
  filterGroups,
  numberGroups,
  openSections,
  parseOpenGroups,
  sectionOfPath,
  serializeOpenGroups,
  toggleGroup,
  withOpenGroup,
  type NumberedDoc,
  type NumberedGroup,
} from "@/lib/docsGroups";

// ── open groups (this browser) ──────────────────────────────────────────────────────────────────

let memoryOpenGroups: string | null = null;
const openGroupsListeners = new Set<() => void>();

function readOpenGroupsRaw(): string | null {
  try {
    return window.localStorage.getItem(DOC_GROUPS_STORAGE_KEY) ?? memoryOpenGroups;
  } catch {
    return memoryOpenGroups;
  }
}

function writeOpenGroups(sections: string[]): void {
  memoryOpenGroups = serializeOpenGroups(sections);
  try {
    window.localStorage.setItem(DOC_GROUPS_STORAGE_KEY, memoryOpenGroups);
  } catch {
    /* storage blocked: the choice lasts for this visit */
  }
  openGroupsListeners.forEach((f) => f());
}

function subscribeOpenGroups(onChange: () => void): () => void {
  openGroupsListeners.add(onChange);
  window.addEventListener("storage", onChange);
  return () => {
    openGroupsListeners.delete(onChange);
    window.removeEventListener("storage", onChange);
  };
}

// ── rows ────────────────────────────────────────────────────────────────────────────────────────

function DocRow({ item, selected, onSelect }: { item: NumberedDoc; selected: boolean; onSelect: () => void }) {
  const t = useT(M);
  const typeLabel = useT(DOC_TYPE_MESSAGES);
  const { doc, number } = item;
  // A link, not a button: the row has a real URL (?path=…), so a middle click or "open in new tab" works too.
  return (
    <a
      href={`/admin/docs?path=${encodeURIComponent(doc.path)}`}
      onClick={(e) => {
        if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
        e.preventDefault();
        onSelect();
      }}
      aria-current={selected ? "page" : undefined}
      data-testid="docs-row"
      style={{
        display: "flex",
        gap: 8,
        padding: "8px 14px 8px 11px",
        background: selected ? K.accentTint : "transparent",
        borderLeft: `3px solid ${selected ? K.accent : "transparent"}`,
        fontFamily: sans,
        color: K.ink,
        textDecoration: "none",
      }}
    >
      <span
        style={{ fontFamily: mono, fontSize: 11, color: K.muted, minWidth: 30, paddingTop: 2, flexShrink: 0 }}
        data-testid="docs-row-number"
      >
        {number}
      </span>
      <span style={{ display: "block", minWidth: 0, flex: 1 }}>
        <span style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 13, fontWeight: 600, minWidth: 0 }}>
          {doc.access === "owner" && <Lock size={12} style={{ color: K.muted, flexShrink: 0 }} aria-label={t("ownerOnly")} />}
          <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{doc.title}</span>
        </span>
        <span style={{ display: "block", fontSize: 11, color: K.muted, fontFamily: mono, marginTop: 2, overflowWrap: "anywhere" }}>
          {doc.id} · {typeLabel(doc.type)}
        </span>
      </span>
    </a>
  );
}

function GroupHeader({
  group, total, open, controls, onToggle,
}: {
  group: NumberedGroup;
  total: number;
  open: boolean;
  controls: string;
  onToggle: () => void;
}) {
  const t = useT(M);
  const navLabel = useT(DOC_NAV_MESSAGES);
  const label = (DOC_NAV_GROUPS as string[]).includes(group.section) ? navLabel(group.section as DocNav) : group.section;
  const shown = group.pages.length;
  const count = shown === total ? t("groupCount", total) : t("groupMatches", shown, total);
  return (
    <Pressable
      onClick={onToggle}
      aria-expanded={open}
      aria-controls={controls}
      title={open ? t("hideGroup", label) : t("showGroup", label)}
      testId="docs-group-toggle"
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        padding: "10px 14px",
        borderTop: `1px solid ${K.line}`,
        fontFamily: sans,
        textAlign: "left",
        color: K.ink,
      }}
    >
      <ChevronRight
        size={14}
        aria-hidden="true"
        style={{ flexShrink: 0, color: K.muted, transform: open ? "rotate(90deg)" : "none", transition: "transform .15s" }}
      />
      <span style={{ fontFamily: mono, fontSize: 12, color: K.muted, flexShrink: 0 }}>{group.number}</span>
      <span
        style={{
          flex: 1, minWidth: 0, overflowWrap: "anywhere", fontSize: 12, fontWeight: 700,
          letterSpacing: "0.04em", textTransform: "uppercase",
        }}
      >
        {label}
      </span>
      <span style={{ fontSize: 11, color: K.muted, flexShrink: 0 }} data-testid="docs-group-count">{count}</span>
    </Pressable>
  );
}

function DocGroup({
  group, total, open, onToggle, isSelected, onOpen,
}: {
  group: NumberedGroup;
  total: number;
  open: boolean;
  onToggle: () => void;
  isSelected: (doc: DocEntry) => boolean;
  onOpen: (path: string) => void;
}) {
  const regionId = useId();
  return (
    <div data-testid="docs-group" data-section={group.section}>
      <GroupHeader group={group} total={total} open={open} controls={regionId} onToggle={onToggle} />
      <div id={regionId} role="group" aria-label={group.section} hidden={!open} style={{ paddingBottom: 4 }}>
        {open &&
          group.pages.map((p) => (
            <DocRow
              key={`${p.doc.repo}/${p.doc.path}`}
              item={p}
              selected={isSelected(p.doc)}
              onSelect={() => onOpen(p.doc.path)}
            />
          ))}
      </div>
    </div>
  );
}

/** AA-1132: the link to the generated "What changed in docs" page, or nothing when the manifest does not list it. */
function ChangesLink({ pages, current, onOpen }: { pages: DocEntry[]; current: string | null; onOpen: (path: string) => void }) {
  const t = useT(M);
  const changes = useMemo(() => docsChangesPage(pages), [pages]);
  if (!changes) return null;
  const selected = changes.path === current;
  return (
    <a
      href={`/admin/docs?path=${encodeURIComponent(changes.path)}`}
      onClick={(e) => {
        if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
        e.preventDefault();
        onOpen(changes.path);
      }}
      aria-current={selected ? "page" : undefined}
      data-testid="docs-changes-link"
      style={{
        display: "flex", alignItems: "center", gap: 8, padding: "8px 14px", borderTop: `1px solid ${K.line}`,
        fontFamily: sans, fontSize: 13, fontWeight: 600, textDecoration: "none",
        color: selected ? K.ink : K.accentDeep, background: selected ? K.accentTint : "transparent",
      }}
    >
      <History size={14} aria-hidden="true" style={{ flexShrink: 0 }} />
      {t("whatChanged")}
    </a>
  );
}

// ── list ────────────────────────────────────────────────────────────────────────────────────────

/** The open groups, remembered per viewer; the open page's group opens each time a new page is opened. */
function useOpenGroups(numbered: NumberedGroup[], openedPath: string | null, selectedSection: string | null) {
  const raw = useSyncExternalStore(subscribeOpenGroups, readOpenGroupsRaw, () => null);
  const stored = useMemo(() => parseOpenGroups(raw), [raw]);
  const open = useMemo(
    () => stored ?? defaultOpenGroups(numbered, selectedSection),
    [stored, numbered, selectedSection],
  );
  // Once per newly opened page, keyed on the page (not its folder): opening another page of a folded group opens
  // that group again, while folding it afterwards still sticks.
  const handled = useRef<string | null>(null);
  useEffect(() => {
    if (!openedPath || !selectedSection || handled.current === openedPath) return;
    handled.current = openedPath;
    const stored = parseOpenGroups(readOpenGroupsRaw());
    if (stored && !stored.includes(selectedSection)) writeOpenGroups(withOpenGroup(stored, selectedSection));
  }, [openedPath, selectedSection]);
  return open;
}

export function DocsList({
  pages, query, type, audience, selected, selectedPath, selectedRepo, onOpen, filters, isPhone, searchPanel,
}: {
  /** Every listed page (originals), in any order. */
  pages: DocEntry[];
  query: string;
  type: string;
  audience: string;
  /** The page being read: its original (a translation's path lists under it), and the path as asked (?path=…). */
  selected: DocEntry | undefined;
  selectedPath: string | null;
  selectedRepo: string;
  onOpen: (path: string) => void;
  /** The search and filter controls, shown above the list. */
  filters: React.ReactNode;
  /** Phone: the list scrolls with the page; wider: the list scrolls in its own box beside the reader. */
  isPhone: boolean;
  /** AA-1133: the full-text search hits, shown instead of the groups while a server search is on; the typed text then
   *  no longer filters the groups (they come back as they were when the search is cleared). */
  searchPanel?: React.ReactNode;
}) {
  const t = useT(M);
  const current = selected?.path ?? selectedPath;
  const numbered = useMemo(() => numberGroups(pages), [pages]);
  const totals = useMemo(() => new Map(numbered.map((g) => [g.section, g.pages.length] as const)), [numbered]);
  const listQuery = searchPanel ? "" : query;
  const groups = useMemo(
    () => filterGroups(numbered, (d) => matchesFilter(d, { query: listQuery, type, audience })),
    [numbered, listQuery, type, audience],
  );
  const shownCount = useMemo(() => groups.reduce((n, g) => n + g.pages.length, 0), [groups]);
  const selectedSection = sectionOfPath(numbered, current);
  const open = useOpenGroups(numbered, current ? `${selectedRepo}/${current}` : null, selectedSection);

  // Groups folded during the current search; a new query starts with none.
  const [folded, setFolded] = useState<{ query: string; sections: string[] }>({ query: "", sections: [] });
  const searching = listQuery.trim() !== "";
  const foldedInSearch = folded.query === listQuery ? folded.sections : [];
  const openSet = openSections({ groups, open, query: listQuery, foldedInSearch });

  const toggle = (section: string) => {
    if (searching) setFolded({ query: listQuery, sections: toggleGroup(foldedInSearch, section) });
    else writeOpenGroups(toggleGroup(open, section));
  };
  const setAll = (expand: boolean) => {
    if (searching) setFolded({ query: listQuery, sections: expand ? [] : groups.map((g) => g.section) });
    else writeOpenGroups(expand ? numbered.map((g) => g.section) : []);
  };
  const isSelected = useCallback(
    (d: DocEntry) => d.path === current && d.repo === selectedRepo,
    [current, selectedRepo],
  );

  return (
    <>
      {filters}
      <div
        style={{
          // AA-1133 follow-up: while search hits show, the hits carry their own count; the page count and the fold
          // buttons speak of the groups, which are not shown then.
          display: searchPanel ? "none" : "flex", flexWrap: "wrap", alignItems: "center", justifyContent: "space-between",
          gap: 8, padding: "8px 14px", fontSize: 12, color: K.muted,
        }}
      >
        <span>{t("countOf", shownCount, pages.length)}</span>
        <span style={{ display: "inline-flex", gap: 6 }}>
          <Button variant="ghost" size="sm" onClick={() => setAll(true)} testId="docs-expand-all">
            {t("expandAll")}
          </Button>
          <Button variant="ghost" size="sm" onClick={() => setAll(false)} testId="docs-collapse-all">
            {t("collapseAll")}
          </Button>
        </span>
      </div>
      <ChangesLink pages={pages} current={current} onOpen={onOpen} />
      <nav aria-label={t("groupsAria")} style={{ maxHeight: isPhone ? "none" : "calc(100vh - 330px)", overflowY: "auto", paddingBottom: 8 }}>
        {searchPanel ? (
          searchPanel
        ) : groups.length ? (
          groups.map((g) => (
            <DocGroup
              key={g.section}
              group={g}
              total={totals.get(g.section) ?? g.pages.length}
              open={openSet.has(g.section)}
              onToggle={() => toggle(g.section)}
              isSelected={isSelected}
              onOpen={onOpen}
            />
          ))
        ) : (
          <EmptyState title={t("noMatch")} description={t("noMatchBody")} />
        )}
      </nav>
    </>
  );
}
